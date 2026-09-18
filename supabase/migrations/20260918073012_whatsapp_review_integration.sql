-- Phase 4: secure WhatsApp review integration.
--
-- Schema + code only: no business data and no authorization rows are seeded.
-- All database changes for this phase live in THIS migration; older
-- migrations are untouched.
--
-- What this migration adds:
--   1. public.biz_review_authorization_audit: append-only audit of every
--      reviewer-authorization transition (granted / updated / revoked).
--      Tenant-scoped idempotency via UNIQUE (tenant_id, request_key).
--   2. public.amose_grant_reviewer(...): creates or idempotently returns an
--      active biz_review_authorizations row for an exact
--      (tenant, business, branch-or-wide, employee) scope with explicit
--      can_confirm / can_reject capabilities. Every effective transition
--      writes one audit row; identical repeats return is_retry with no new
--      rows. Callable only through the trusted administrative boundary
--      (service_role EXECUTE only); never reachable from chat input.
--   3. public.amose_revoke_reviewer(...): deactivates the exact-scope grant
--      (history preserved) plus one audit row; repeats report
--      already_revoked with is_retry and write nothing.
--   4. public.amose_list_reviewer_authorizations(...): restricted,
--      tenant-scoped read of active (optionally all) grants as JSON. This
--      is the ONLY read path for authorization configuration: no role gets
--      direct table SELECT.
--   5. public.amose_queue_review_requests(...): after a valid draft exists,
--      issues (or reuses) its opaque YR- review reference and queues one
--      WhatsApp review_request outbound row per eligible reviewer. The
--      message carries only safe business context, proposed facts, the
--      opaque reference, and strict command instructions -- never UUIDs,
--      secrets, or raw payloads. Money renders from integer kobo to naira
--      with exact integer arithmetic (see _amose_format_kobo). Outcomes
--      are explicit and never report success when nobody was notified:
--      queued (new notification), already_queued (idempotent retry),
--      no_eligible_reviewer (no active authorized reviewer), or
--      reviewer_unroutable (authorization exists but no authoritative
--      WhatsApp identity or provider-account routing). Per-(case,
--      reviewer) idempotency keys make re-runs duplicate-free, and the
--      draft stays draft, so it remains safely reviewable and requeueable
--      after configuration is corrected.
--   6. public.amose_review_confirm_command(...): chat-confirm path. Resolves
--      the sender identity, the reference (inside the reviewer's own
--      tenant), and explicit authorization plus separation of duties using
--      the Phase 3 helpers, builds the verified block deterministically
--      from the submission's own parsed extraction (sale reports only;
--      exactly one active product must match, amounts must be integral),
--      then performs the standard Phase 3 confirm transaction. Exactly one
--      write boundary; Python performs zero direct writes.
--
-- Rejections from chat need no new RPC: callers use the Phase 3
-- amose_reject_submission reference RPC directly (reason travels inline).
--
-- Outbound change (why): review_request rows, like the Phase 3
-- acknowledgements, are not caused by a task, so related_task_id stays
-- NULL for them. The guard CHECK is extended (in this migration only) so
-- that ONLY review_confirmed / review_rejected / review_request may have
-- a NULL task -- every other message type still requires one.
--
-- Security model: RLS enabled on the new audit table with no policies
-- (fail closed); ALL revoked from PUBLIC/anon/authenticated/service_role
-- first, then service_role gets SELECT + INSERT only (no UPDATE, DELETE,
-- TRUNCATE, REFERENCES, TRIGGER, or MAINTAIN). The authorization and case
-- tables keep their Phase 3 posture (no caller-role access at all). New
-- helpers are owner-only; new public RPCs are executable by service_role
-- only. All SECURITY DEFINER functions use a fixed search_path
-- (pg_catalog) with schema-qualified references.
begin;

-- ---------------------------------------------------------------------------
-- 1. Append-only authorization-change audit. Composite FKs keep every row
-- inside its own tenant/business/branch/employee scope; idempotency is
-- tenant-scoped so one tenant can never reserve another tenant's key.
-- ---------------------------------------------------------------------------
create table public.biz_review_authorization_audit (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text,
  employee_id uuid not null,
  action text not null check (action in ('granted', 'updated', 'revoked')),
  can_confirm boolean not null,
  can_reject boolean not null,
  actor text,
  reason text,
  request_key text not null,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, employee_id)
    references public.biz_employees(tenant_id, id),
  check (business_id is not null or branch_id is null),
  check (action <> 'revoked' or (can_confirm = false and can_reject = false)),
  unique (tenant_id, request_key)
);
comment on table public.biz_review_authorization_audit is
  'Append-only reviewer-authorization transitions. State changes only; idempotent repeats write nothing.';
create index biz_review_authorization_audit_scope
  on public.biz_review_authorization_audit(tenant_id, business_id, employee_id, created_at);

-- ---------------------------------------------------------------------------
-- 2. Outbound guard CHECK: review_request joins the task-less family.
-- ---------------------------------------------------------------------------
alter table public.biz_outbound_messages
  drop constraint biz_outbound_messages_review_task_check;
alter table public.biz_outbound_messages
  add constraint biz_outbound_messages_review_task_check
  check ((message_type in ('review_confirmed', 'review_rejected', 'review_request'))
    = (related_task_id is null));

-- ---------------------------------------------------------------------------
-- 3. Grant reviewer authorization. Exact scope, explicit capabilities, fully
-- audited, idempotent. The employee must exist AND be active in the tenant;
-- anything else fails closed (an inactive grantee could never act, so the
-- grant would be meaningless). Owner-only caller surface: service_role
-- EXECUTE via the trusted administrative boundary only.
-- ---------------------------------------------------------------------------
create or replace function public.amose_grant_reviewer(
  p_tenant_id uuid,
  p_business_id text,
  p_branch_id text,
  p_employee_id uuid,
  p_can_confirm boolean,
  p_can_reject boolean,
  p_request_key text,
  p_reason text default null,
  p_actor text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_key text;
  v_reason text;
  v_actor text;
  v_grant_id uuid;
  v_out jsonb;
begin
  if p_tenant_id is null then
    raise exception 'MALFORMED: tenant id is required';
  end if;
  if nullif(btrim(p_business_id), '') is null then
    raise exception 'MALFORMED: business id is required';
  end if;
  if p_employee_id is null then
    raise exception 'MALFORMED: employee id is required';
  end if;
  if p_can_confirm is null or p_can_reject is null then
    raise exception 'MALFORMED: capability flags are required';
  end if;
  if not (p_can_confirm or p_can_reject) then
    raise exception 'MALFORMED: at least one capability is required';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_reason := nullif(btrim(p_reason), '');
  if v_reason is not null and pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: reason is too long';
  end if;
  v_actor := nullif(btrim(p_actor), '');
  if v_actor is not null and pg_catalog.char_length(v_actor) > 200 then
    raise exception 'MALFORMED: actor is too long';
  end if;

  -- Scope must exist: business in tenant, branch (when given) in business.
  if not exists (select 1 from public.biz_businesses b
      where b.tenant_id = p_tenant_id and b.id = btrim(p_business_id)) then
    raise exception 'NOT_FOUND: business % does not exist in this tenant',
      p_business_id;
  end if;
  if p_branch_id is not null and nullif(btrim(p_branch_id), '') is not null
      and not exists (select 1 from public.biz_branches b
        where b.tenant_id = p_tenant_id and b.business_id = btrim(p_business_id)
          and b.id = btrim(p_branch_id)) then
    raise exception 'NOT_FOUND: branch % does not exist in this business',
      p_branch_id;
  end if;
  if p_branch_id is not null and nullif(btrim(p_branch_id), '') is null then
    raise exception 'MALFORMED: branch id must be null or non-blank';
  end if;
  -- Grantee must be an active employee of the tenant.
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = p_tenant_id and e.id = p_employee_id
        and e.active = true) then
    raise exception 'NOT_FOUND: employee % is not active in this tenant',
      p_employee_id;
  end if;

  -- Tenant-scoped idempotency first: the identical administrative act
  -- returns its stored result; a recycled key with different data fails.
  perform 1
    from public.biz_review_authorization_audit a
    where a.tenant_id = p_tenant_id
      and a.request_key = v_key;
  if found then
    select jsonb_build_object('status', 'ok', 'action', a.action,
        'grant', jsonb_build_object(
          'tenant_id', a.tenant_id::text,
          'business_id', a.business_id,
          'branch_id', a.branch_id,
          'employee_id', a.employee_id::text,
          'can_confirm', a.can_confirm,
          'can_reject', a.can_reject),
        'request_key', v_key, 'is_retry', true)
      into v_out
      from public.biz_review_authorization_audit a
      where a.tenant_id = p_tenant_id
        and a.request_key = v_key
        and a.business_id = btrim(p_business_id)
        and a.branch_id is not distinct from
          nullif(btrim(p_branch_id), '')
        and a.employee_id = p_employee_id
        and a.action in ('granted', 'updated')
        and a.can_confirm is not distinct from p_can_confirm
        and a.can_reject is not distinct from p_can_reject;
    if found then
      return v_out;
    end if;
    raise exception 'CONFLICT: request key was already used for a different authorization act';
  end if;

  -- An identical ACTIVE grant already exists: report it, change nothing.
  select g.id into v_grant_id
    from public.biz_review_authorizations g
    where g.tenant_id = p_tenant_id
      and g.business_id = btrim(p_business_id)
      and g.branch_id is not distinct from nullif(btrim(p_branch_id), '')
      and g.employee_id = p_employee_id
      and g.active = true
      and g.can_confirm is not distinct from p_can_confirm
      and g.can_reject is not distinct from p_can_reject;
  if found then
    return jsonb_build_object('status', 'ok', 'action', 'granted',
      'grant', jsonb_build_object(
        'tenant_id', p_tenant_id::text,
        'business_id', btrim(p_business_id),
        'branch_id', nullif(btrim(p_branch_id), ''),
        'employee_id', p_employee_id::text,
        'can_confirm', p_can_confirm,
        'can_reject', p_can_reject),
      'request_key', v_key, 'is_retry', true);
  end if;

  -- An active grant with different capabilities: update it (audited).
  update public.biz_review_authorizations g
    set can_confirm = p_can_confirm,
      can_reject = p_can_reject,
      updated_at = pg_catalog.now()
    where g.tenant_id = p_tenant_id
      and g.business_id = btrim(p_business_id)
      and g.branch_id is not distinct from nullif(btrim(p_branch_id), '')
      and g.employee_id = p_employee_id
      and g.active = true
    returning g.id into v_grant_id;
  if found then
    insert into public.biz_review_authorization_audit
      (tenant_id, business_id, branch_id, employee_id, action,
       can_confirm, can_reject, actor, reason, request_key)
      values (p_tenant_id, btrim(p_business_id),
        nullif(btrim(p_branch_id), ''), p_employee_id, 'updated',
        p_can_confirm, p_can_reject, v_actor, v_reason, v_key);
    return jsonb_build_object('status', 'ok', 'action', 'updated',
      'grant', jsonb_build_object(
        'tenant_id', p_tenant_id::text,
        'business_id', btrim(p_business_id),
        'branch_id', nullif(btrim(p_branch_id), ''),
        'employee_id', p_employee_id::text,
        'can_confirm', p_can_confirm,
        'can_reject', p_can_reject),
      'request_key', v_key, 'is_retry', false);
  end if;

  -- Fresh grant (reactivating history is a new row; old rows stay).
  insert into public.biz_review_authorizations
    (tenant_id, business_id, branch_id, employee_id,
     can_confirm, can_reject, active)
    values (p_tenant_id, btrim(p_business_id),
      nullif(btrim(p_branch_id), ''), p_employee_id,
      p_can_confirm, p_can_reject, true)
    returning id into v_grant_id;
  insert into public.biz_review_authorization_audit
    (tenant_id, business_id, branch_id, employee_id, action,
     can_confirm, can_reject, actor, reason, request_key)
    values (p_tenant_id, btrim(p_business_id),
      nullif(btrim(p_branch_id), ''), p_employee_id, 'granted',
      p_can_confirm, p_can_reject, v_actor, v_reason, v_key);
  return jsonb_build_object('status', 'ok', 'action', 'granted',
    'grant', jsonb_build_object(
      'tenant_id', p_tenant_id::text,
      'business_id', btrim(p_business_id),
      'branch_id', nullif(btrim(p_branch_id), ''),
      'employee_id', p_employee_id::text,
      'can_confirm', p_can_confirm,
      'can_reject', p_can_reject),
    'request_key', v_key, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 4. Revoke reviewer authorization. Deactivates the exact-scope grant
-- (history is preserved, never deleted) plus one audit row. Repeats report
-- already_revoked with is_retry and write nothing. Same tenant-scoped key
-- discipline as the grant RPC.
-- ---------------------------------------------------------------------------
create or replace function public.amose_revoke_reviewer(
  p_tenant_id uuid,
  p_business_id text,
  p_branch_id text,
  p_employee_id uuid,
  p_request_key text,
  p_reason text default null,
  p_actor text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_key text;
  v_reason text;
  v_actor text;
  v_grant_id uuid;
begin
  if p_tenant_id is null then
    raise exception 'MALFORMED: tenant id is required';
  end if;
  if nullif(btrim(p_business_id), '') is null then
    raise exception 'MALFORMED: business id is required';
  end if;
  if p_employee_id is null then
    raise exception 'MALFORMED: employee id is required';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_reason := nullif(btrim(p_reason), '');
  if v_reason is not null and pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: reason is too long';
  end if;
  v_actor := nullif(btrim(p_actor), '');
  if v_actor is not null and pg_catalog.char_length(v_actor) > 200 then
    raise exception 'MALFORMED: actor is too long';
  end if;

  -- Tenant-scoped idempotency: the identical revocation returns its stored
  -- result; a recycled key with different data fails closed.
  perform 1
    from public.biz_review_authorization_audit a
    where a.tenant_id = p_tenant_id
      and a.request_key = v_key;
  if found then
    perform 1
      from public.biz_review_authorization_audit a
      where a.tenant_id = p_tenant_id
        and a.request_key = v_key
        and a.business_id = btrim(p_business_id)
        and a.branch_id is not distinct from
          nullif(btrim(p_branch_id), '')
        and a.employee_id = p_employee_id
        and a.action = 'revoked';
    if found then
      return jsonb_build_object('status', 'ok', 'action', 'revoked',
        'grant', jsonb_build_object(
          'tenant_id', p_tenant_id::text,
          'business_id', btrim(p_business_id),
          'branch_id', nullif(btrim(p_branch_id), ''),
          'employee_id', p_employee_id::text,
          'can_confirm', false,
          'can_reject', false),
        'request_key', v_key, 'is_retry', true);
    end if;
    raise exception 'CONFLICT: request key was already used for a different authorization act';
  end if;

  update public.biz_review_authorizations g
    set active = false,
      updated_at = pg_catalog.now()
    where g.tenant_id = p_tenant_id
      and g.business_id = btrim(p_business_id)
      and g.branch_id is not distinct from nullif(btrim(p_branch_id), '')
      and g.employee_id = p_employee_id
      and g.active = true
    returning g.id into v_grant_id;
  if not found then
    return jsonb_build_object('status', 'ok', 'action', 'already_revoked',
      'grant', jsonb_build_object(
        'tenant_id', p_tenant_id::text,
        'business_id', btrim(p_business_id),
        'branch_id', nullif(btrim(p_branch_id), ''),
        'employee_id', p_employee_id::text,
        'can_confirm', false,
        'can_reject', false),
      'request_key', v_key, 'is_retry', true);
  end if;

  insert into public.biz_review_authorization_audit
    (tenant_id, business_id, branch_id, employee_id, action,
     can_confirm, can_reject, actor, reason, request_key)
    values (p_tenant_id, btrim(p_business_id),
      nullif(btrim(p_branch_id), ''), p_employee_id, 'revoked',
      false, false, v_actor, v_reason, v_key);
  return jsonb_build_object('status', 'ok', 'action', 'revoked',
    'grant', jsonb_build_object(
      'tenant_id', p_tenant_id::text,
      'business_id', btrim(p_business_id),
      'branch_id', nullif(btrim(p_branch_id), ''),
      'employee_id', p_employee_id::text,
      'can_confirm', false,
      'can_reject', false),
    'request_key', v_key, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 5. List reviewer authorizations. The ONLY read path for authorization
-- configuration: no role gets direct table SELECT. Tenant-scoped with
-- optional business/branch narrowing (a branch filter requires its
-- business, mirroring the schema's branch-without-business rule).
-- ---------------------------------------------------------------------------
create or replace function public.amose_list_reviewer_authorizations(
  p_tenant_id uuid,
  p_business_id text default null,
  p_branch_id text default null,
  p_include_inactive boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_business text;
  v_branch text;
  v_out jsonb;
begin
  if p_tenant_id is null then
    raise exception 'MALFORMED: tenant id is required';
  end if;
  if not exists (select 1 from public.biz_tenants t
      where t.id = p_tenant_id) then
    raise exception 'NOT_FOUND: tenant does not exist';
  end if;
  v_business := nullif(btrim(p_business_id), '');
  v_branch := nullif(btrim(p_branch_id), '');
  if v_branch is not null and v_business is null then
    raise exception 'MALFORMED: a branch filter requires its business';
  end if;
  if v_business is not null
      and not exists (select 1 from public.biz_businesses b
        where b.tenant_id = p_tenant_id and b.id = v_business) then
    raise exception 'NOT_FOUND: business % does not exist in this tenant',
      p_business_id;
  end if;
  if v_branch is not null
      and not exists (select 1 from public.biz_branches b
        where b.tenant_id = p_tenant_id and b.business_id = v_business
          and b.id = v_branch) then
    raise exception 'NOT_FOUND: branch % does not exist in this business',
      p_branch_id;
  end if;

  select coalesce(jsonb_agg(to_jsonb(listed)), '[]'::jsonb)
    into v_out
    from (select g.employee_id::text as employee_id,
        g.business_id as business_id,
        g.branch_id as branch_id,
        g.can_confirm as can_confirm,
        g.can_reject as can_reject,
        g.active as active,
        g.updated_at as updated_at
      from public.biz_review_authorizations g
      where g.tenant_id = p_tenant_id
        and (v_business is null or g.business_id = v_business)
        and (v_branch is null
          or (g.branch_id is null or g.branch_id = v_branch))
        and (p_include_inactive is true or g.active = true)
      order by g.business_id, g.branch_id nulls first, g.employee_id) as listed;
  return jsonb_build_object('status', 'ok', 'tenant_id', p_tenant_id::text,
    'authorizations', v_out);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 6. Internal helpers: integer-safe money formatting and the safe
-- review-request message. All money is stored in integer kobo and rendered
-- in naira with exact integer arithmetic only (kobo / 100 + remainder,
-- zero-padded): 50000 kobo -> ₦500.00, 100 -> ₦1.00, 1 -> ₦0.01. No
-- floating point anywhere; raw kobo is never labeled NGN or naira.
-- The message carries only business and branch codes, kind-derived
-- proposed facts from the parsed extraction, the opaque reference, and
-- strict command instructions. NEVER UUIDs, secrets, raw payloads, phone
-- numbers, or amounts beyond the proposal totals. Every interpolated value
-- is length-capped. Owner-only.
-- ---------------------------------------------------------------------------
create or replace function public._amose_format_kobo(p_amount numeric)
returns text
language plpgsql
immutable
security definer
set search_path = pg_catalog
as $func$
declare
  v_kobo bigint;
  v_sign text := '';
  v_abs bigint;
begin
  -- Only exact whole-kobo values render: NULL, fractional, and
  -- out-of-bigint-range inputs yield NULL so callers fall back to '?'
  -- instead of printing a wrong amount.
  if p_amount is null or p_amount <> trunc(p_amount) then
    return null;
  end if;
  if p_amount < -9223372036854775807 or p_amount > 9223372036854775807 then
    return null;
  end if;
  v_kobo := p_amount::bigint;
  if v_kobo < 0 then
    v_sign := '-';
    v_abs := -v_kobo;
  else
    v_abs := v_kobo;
  end if;
  return v_sign || '₦' || (v_abs / 100)::text || '.'
    || lpad((v_abs % 100)::text, 2, '0');
end;
$func$;
create or replace function public._amose_review_request_text(
  p_kind text,
  p_business_id text,
  p_branch_id text,
  p_parsed jsonb,
  p_review_ref text
)
returns text
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_fields jsonb;
  v_lines text := '';
  v_missing text;
  v_text text;
  v_raw text;
  v_money text;
begin
  if p_kind = 'sale' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'unit_price', '');
    v_money := case when v_raw ~ '^-?[0-9]+(\.[0-9]+)?$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Sale proposal: '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'quantity', ''), 24), '?')
      || ' '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'unit', ''), 24), 'units')
      || ' at '
      || coalesce(v_money, '?');
    if p_parsed ? 'missing_fields'
        and jsonb_typeof(p_parsed -> 'missing_fields') = 'array'
        and jsonb_array_length(p_parsed -> 'missing_fields') > 0 then
      select 'Missing: ' || pg_catalog.left(
          (select string_agg(value::text, ', ')
            from jsonb_array_elements_text(p_parsed -> 'missing_fields') as value),
          160)
        into v_missing;
      v_lines := v_lines || '. ' || coalesce(v_missing, '');
    end if;
  elsif p_kind = 'production' or p_kind = 'poultry_daily_report' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_lines := 'Production proposal: good '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'good_quantity',
        ''), 24), '?')
      || ', rejected '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'rejected_quantity',
        ''), 24), '0')
      || ', date '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'production_date',
        ''), 16), '?')
      || coalesce(', shift ' || pg_catalog.left(nullif(v_fields ->> 'shift',
        ''), 16), '');
  elsif p_kind = 'payment' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+(\.[0-9]+)?$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Payment proposal: amount '
      || coalesce(v_money, '?')
      || ', method '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'method',
        ''), 24), '?');
  elsif p_kind = 'expense' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+(\.[0-9]+)?$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Expense proposal: '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'category',
        ''), 24), '?')
      || ', amount '
      || coalesce(v_money, '?');
  else
    v_lines := 'Report proposal (' || pg_catalog.left(p_kind, 40) || ')';
  end if;

  v_text := 'Review request ' || p_review_ref || ' for '
    || pg_catalog.left(p_business_id, 64) || '/'
    || pg_catalog.left(p_branch_id, 64) || ': ' || v_lines || '. ';
  if p_kind = 'sale' then
    v_text := v_text || 'Reply REVIEW CONFIRM ' || p_review_ref
      || ' KEY your-unique-key to confirm, or REVIEW REJECT ' || p_review_ref
      || ' KEY your-unique-key REASON why to reject.';
  else
    v_text := v_text || 'Reply REVIEW REJECT ' || p_review_ref
      || ' KEY your-unique-key REASON why to reject. '
      || 'Chat confirm supports sale reports; this kind needs API review.';
  end if;
  return v_text;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 7. Queue WhatsApp review requests for a draft submission. Issues (or
-- reuses) the single opaque reference, then queues one review_request row
-- per eligible reviewer (active authorization with confirm or reject
-- capability, active employee, verified WhatsApp identity). The reporter is
-- skipped (they filed it); identity-less reviewers are skipped (never queue
-- a row that cannot be delivered). Outcomes are explicit -- queued,
-- already_queued, no_eligible_reviewer, reviewer_unroutable -- and success
-- is never reported when nobody was notified. Per-(case, reviewer)
-- idempotency keys make re-runs duplicate-free, and the draft stays draft.
-- ---------------------------------------------------------------------------
create or replace function public.amose_queue_review_requests(
  p_submission_id uuid,
  p_request_key text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text; v_payload jsonb; v_inbox uuid; v_parsed jsonb;
  v_key text;
  v_ref text;
  v_acct text;
  v_msg text;
  v_notified int := 0;
  v_eligible int := 0;
  v_skipped jsonb := '[]'::jsonb;
  v_preexisting boolean := false;
  v_attempts int := 0;
  r record;
  v_sender text;
begin
  if p_submission_id is null then
    raise exception 'NOT_FOUND: submission id is required';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;

  -- Lock first so concurrent queue attempts serialize on this row.
  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.payload, s.inbox_id
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_payload, v_inbox
    from public.biz_submissions s
    where s.id = p_submission_id
    for update;
  if not found then
    raise exception 'NOT_FOUND: submission % does not exist', p_submission_id;
  end if;
  if v_status is distinct from 'draft' then
    raise exception 'NOT_REVIEWABLE: submission % has status %, only draft submissions need review',
      v_sub, v_status;
  end if;
  if v_kind not in ('production', 'poultry_daily_report', 'sale',
      'payment', 'expense') then
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, which needs no human review',
      v_sub, v_kind;
  end if;
  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;

  -- Issue (or reuse) the single reference for this draft.
  select c.review_ref into v_ref
    from public.biz_review_cases c
    where c.tenant_id = v_tenant
      and c.business_id = v_business
      and c.branch_id = v_branch
      and c.submission_id = v_sub;
  if not found then
    loop
      v_attempts := v_attempts + 1;
      v_ref := public._amose_new_review_ref();
      begin
        insert into public.biz_review_cases
          (tenant_id, business_id, branch_id, submission_id,
           review_ref, request_key)
          values (v_tenant, v_business, v_branch, v_sub,
            v_ref, 'queue:' || v_key);
        exit;
      exception when unique_violation then
        select c.review_ref into v_ref
          from public.biz_review_cases c
          where c.tenant_id = v_tenant
            and c.business_id = v_business
            and c.branch_id = v_branch
            and c.submission_id = v_sub;
        if found then
          exit;
        end if;
        if v_attempts >= 8 then
          raise exception 'INCOMPLETE: could not mint a unique review reference';
        end if;
      end;
    end loop;
  end if;

  -- Outcome discipline (never report success when nobody was notified):
  -- no_eligible_reviewer (no active authorized reviewer besides the
  -- reporter), reviewer_unroutable (authorization exists but no
  -- authoritative WhatsApp identity or provider-account routing),
  -- queued (at least one new notification), already_queued (idempotent
  -- retry: notifications already exist, nothing duplicated). The draft
  -- stays draft in every non-error case, so it remains safely reviewable
  -- and requeueable after configuration is corrected.
  select count(*) into v_eligible
    from public.biz_review_authorizations a
    join public.biz_employees e
      on e.tenant_id = a.tenant_id and e.id = a.employee_id
    where a.tenant_id = v_tenant
      and a.business_id = v_business
      and (a.branch_id is null or a.branch_id = v_branch)
      and a.active = true
      and (a.can_confirm or a.can_reject)
      and e.active = true
      and a.employee_id is distinct from v_submitter;
  if v_eligible = 0 then
    return jsonb_build_object('status', 'no_eligible_reviewer',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', 0,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;

  -- Authoritative routing: the business's own number snapshot must exist
  -- before any row is written. Missing routing is a reported outcome, not
  -- an exception, so callers can distinguish it from a failure.
  if v_inbox is null then
    return jsonb_build_object('status', 'reviewer_unroutable',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', 0,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;
  select nullif(btrim(i.provider_account), '') into v_acct
    from public.biz_message_inbox i
    where i.id = v_inbox;
  if v_acct is null then
    return jsonb_build_object('status', 'reviewer_unroutable',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', 0,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;

  -- A re-run that already notified reviewers is idempotent: per-reviewer
  -- keys make re-inserts no-ops, so late-granted reviewers still get
  -- notified while nobody is ever double-notified.
  select exists (select 1 from public.biz_outbound_messages o
      where o.idempotency_key
        like 'review_req:' || v_tenant::text || ':' || v_ref || ':%')
    into v_preexisting;

  v_msg := public._amose_review_request_text(
    v_kind, v_business, v_branch, v_parsed, v_ref);

  for r in
    select a.employee_id as employee_id
      from public.biz_review_authorizations a
      join public.biz_employees e
        on e.tenant_id = a.tenant_id and e.id = a.employee_id
      where a.tenant_id = v_tenant
        and a.business_id = v_business
        and (a.branch_id is null or a.branch_id = v_branch)
        and a.active = true
        and (a.can_confirm or a.can_reject)
        and e.active = true
        and a.employee_id is distinct from v_submitter
      order by a.employee_id
  loop
    select i.provider_sender into v_sender
      from public.biz_sender_identities i
      where i.tenant_id = v_tenant
        and i.employee_id = r.employee_id
        and i.provider = 'whatsapp'
      order by i.provider_sender
      limit 1;
    if v_sender is null then
      v_skipped := v_skipped || jsonb_build_object(
        'employee_id', r.employee_id::text, 'reason', 'no_whatsapp_identity');
      continue;
    end if;
    begin
      insert into public.biz_outbound_messages
        (tenant_id, business_id, branch_id, recipient_employee_id,
         provider, provider_sender, provider_account,
         related_task_id, message_type, message_text, status, idempotency_key)
        values (v_tenant, v_business, v_branch, r.employee_id,
          'whatsapp', v_sender, v_acct,
          null, 'review_request', v_msg,
          'queued', 'review_req:' || v_tenant::text || ':' || v_ref
            || ':' || r.employee_id::text);
      v_notified := v_notified + 1;
    exception when unique_violation then
      -- Already notified on an earlier run: not an error, not a duplicate.
      null;
    end;
  end loop;

  -- Skipped reporters are worth naming (they hold grants but filed this).
  for r in
    select a.employee_id as employee_id
      from public.biz_review_authorizations a
      where a.tenant_id = v_tenant
        and a.business_id = v_business
        and (a.branch_id is null or a.branch_id = v_branch)
        and a.active = true
        and (a.can_confirm or a.can_reject)
        and a.employee_id = v_submitter
  loop
    v_skipped := v_skipped || jsonb_build_object(
      'employee_id', r.employee_id::text, 'reason', 'reporter');
  end loop;

  if v_notified > 0 then
    return jsonb_build_object('status', 'queued',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', v_notified,
      'skipped', v_skipped,
      'is_retry', v_preexisting);
  end if;
  if v_preexisting then
    select count(*) into v_notified
      from public.biz_outbound_messages o
      where o.idempotency_key
        like 'review_req:' || v_tenant::text || ':' || v_ref || ':%';
    return jsonb_build_object('status', 'already_queued',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', v_notified,
      'skipped', v_skipped,
      'is_retry', true);
  end if;
  return jsonb_build_object('status', 'reviewer_unroutable',
    'review_ref', v_ref,
    'submission_id', v_sub::text,
    'submission_kind', v_kind,
    'request_key', v_key,
    'notified', 0,
    'skipped', v_skipped,
    'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 8. Chat-confirm path. Resolves sender identity, the reference (inside the
-- reviewer's own tenant), and explicit confirm authorization plus
-- separation of duties with the Phase 3 helpers; builds the verified block
-- deterministically from the submission's own parsed extraction (sale
-- reports only: amounts must be integral and exactly one active product
-- must match the reported unit -- anything else fails closed); then runs
-- the standard Phase 3 confirm transaction. Exactly one write boundary.
-- ---------------------------------------------------------------------------
create or replace function public.amose_review_confirm_command(
  p_review_ref text,
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_request_key text,
  p_correction_reason text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_reviewer uuid;
  v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text; v_payload jsonb;
  v_ref text; v_key text; v_reason text;
  v_parsed jsonb; v_fields jsonb;
  v_unit text; v_qty numeric; v_price numeric;
  v_product uuid; v_product_count int;
  v_verified jsonb;
begin
  v_ref := nullif(btrim(p_review_ref), '');
  if v_ref is null or v_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_reason := nullif(btrim(p_correction_reason), '');
  if v_reason is not null and pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: correction reason is too long';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, p_reviewer_sender) as o;

  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.payload
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_payload
    from public.biz_submissions s
    join public.biz_review_cases c
      on c.tenant_id = v_tenant
      and c.review_ref = v_ref
      and s.tenant_id = c.tenant_id
      and s.business_id = c.business_id
      and s.branch_id = c.branch_id
      and s.id = c.submission_id
    for update of s;
  if not found then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_business, v_branch, v_submitter, 'confirm');

  -- Only sale reports are confirmable from chat: their parsed facts map
  -- exactly onto the verified sale schema. Anything else needs API review
  -- (reject-from-chat still works for every kind via the Phase 3 RPC).
  if v_kind is distinct from 'sale' then
    raise exception 'MALFORMED: only sale reports can be confirmed from chat';
  end if;
  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;
  v_fields := case when jsonb_typeof(v_parsed -> 'fields') = 'object'
    then v_parsed -> 'fields' else '{}'::jsonb end;
  v_unit := nullif(btrim(v_fields ->> 'unit'), '');
  if v_unit is null then
    raise exception 'MALFORMED: parsed sale has no unit to match a product';
  end if;
  begin
    v_qty := (v_fields ->> 'quantity')::numeric;
    v_price := (v_fields ->> 'unit_price')::numeric;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: parsed sale amounts are not numeric';
  end;
  if v_qty is null or v_price is null
      or v_qty <> trunc(v_qty) or v_price <> trunc(v_price) then
    raise exception 'MALFORMED: parsed sale amounts are not whole numbers';
  end if;
  if v_qty < 1 or v_qty > 2147483647 then
    raise exception 'MALFORMED: parsed sale quantity is out of range';
  end if;
  if v_price < 0 or v_price > 9223372036854775807 then
    raise exception 'MALFORMED: parsed sale price is out of range';
  end if;
  -- Deterministic product match, never a guess: exactly one active product
  -- with this base unit in the submission's own business.
  select count(*) into v_product_count
    from public.biz_products p
    where p.tenant_id = v_tenant and p.business_id = v_business
      and p.base_unit = v_unit and p.is_active = true;
  if v_product_count <> 1 then
    raise exception 'MALFORMED: no single active product matches the reported unit';
  end if;
  select p.id into v_product
    from public.biz_products p
    where p.tenant_id = v_tenant and p.business_id = v_business
      and p.base_unit = v_unit and p.is_active = true;

  v_verified := jsonb_build_object('kind', 'sale', 'lines',
    jsonb_build_array(jsonb_build_object(
      'product_id', v_product::text,
      'quantity', v_qty::int,
      'unit_price_kobo', v_price::bigint,
      'storage_state', 'normal')));

  return public.amose_confirm_submission(
    v_ref, p_reviewer_provider, p_reviewer_sender,
    v_verified, v_key, v_reason);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 9. RLS + least privilege. Fail closed for every caller role.
--
-- Privilege matrix (this migration; Phase 3 posture unchanged):
--   biz_review_authorization_audit: RLS ON, no policies;
--     PUBLIC/anon/authenticated: NO privileges;
--     service_role: SELECT + INSERT only
--       (no UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN).
--   biz_review_authorizations / biz_review_cases / biz_review_audit:
--     unchanged (no caller-role access; SECURITY DEFINER only).
--   Internal helpers (_amose_format_kobo, _amose_review_request_text):
--     EXECUTE revoked from PUBLIC/anon/authenticated/service_role
--     (owner-only).
--   Public RPCs (amose_grant_reviewer, amose_revoke_reviewer,
--     amose_list_reviewer_authorizations, amose_queue_review_requests,
--     amose_review_confirm_command): EXECUTE revoked from
--     PUBLIC/anon/authenticated; granted to service_role only.
--
-- The rewrite-guard trigger aborts UPDATE/DELETE on the audit table for
-- every role, including the table owner.
-- ---------------------------------------------------------------------------
alter table public.biz_review_authorization_audit enable row level security;

revoke all on public.biz_review_authorization_audit
  from public, anon, authenticated, service_role;

grant select, insert on public.biz_review_authorization_audit to service_role;

revoke truncate on public.biz_review_authorization_audit from service_role;

create trigger biz_review_authorization_audit_no_rewrite
  before update or delete on public.biz_review_authorization_audit
  for each row execute function public.reject_history_rewrite();

revoke execute on function public._amose_format_kobo(numeric)
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_review_request_text(text, text, text, jsonb, text)
  from public, anon, authenticated, service_role;

revoke execute on function public.amose_grant_reviewer(uuid, text, text, uuid, boolean, boolean, text, text, text)
  from public, anon, authenticated;
revoke execute on function public.amose_revoke_reviewer(uuid, text, text, uuid, text, text, text)
  from public, anon, authenticated;
revoke execute on function public.amose_list_reviewer_authorizations(uuid, text, text, boolean)
  from public, anon, authenticated;
revoke execute on function public.amose_queue_review_requests(uuid, text)
  from public, anon, authenticated;
revoke execute on function public.amose_review_confirm_command(text, text, text, text, text)
  from public, anon, authenticated;

grant execute on function public.amose_grant_reviewer(uuid, text, text, uuid, boolean, boolean, text, text, text)
  to service_role;
grant execute on function public.amose_revoke_reviewer(uuid, text, text, uuid, text, text, text)
  to service_role;
grant execute on function public.amose_list_reviewer_authorizations(uuid, text, text, boolean)
  to service_role;
grant execute on function public.amose_queue_review_requests(uuid, text)
  to service_role;
grant execute on function public.amose_review_confirm_command(text, text, text, text, text)
  to service_role;

commit;
