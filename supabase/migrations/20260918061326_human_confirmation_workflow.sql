-- Phase 3: controlled human confirmation and WhatsApp review workflow
-- (final security correction).
--
-- Schema + code only: no data rows are seeded and no business-policy VALUES
-- are inserted. Money stays integer kobo (bigint); floats are never used.
--
-- What this migration adds:
--   1. public.biz_review_authorizations: the ONLY reviewer authorization
--      source. Ordinary biz_assignments membership (which every assigned
--      employee holds, including the reporter) never authorizes a review.
--      Scope is tenant/business with an optional branch: NULL branch means
--      explicitly business-wide. No authorization rows are seeded here.
--   2. public.biz_review_cases: one opaque review reference (YR-XXXXXXXXXX)
--      per reviewable draft submission. Internal submission UUIDs never
--      appear in WhatsApp commands; the reference resolves to the locked
--      submission inside the database.
--   3. public.biz_review_audit: normalized append-only review/audit history.
--      One row per reviewed submission action (corrected / confirmed /
--      rejected). Corrections never overwrite the raw message or the parsed
--      submission facts: the original payload stays intact and the reviewer
--      supplied facts live only in payload.verified plus the immutable
--      verified_snapshot stored here. Idempotency is tenant-scoped:
--      UNIQUE (tenant_id, request_key).
--   4. public.amose_issue_review_reference(...): issues (or idempotently
--      re-returns) the single review reference for a draft submission.
--      Terminal confirmed/rejected submissions cannot receive a reference.
--   5. public.amose_confirm_submission(...): ONE atomic transaction that
--      resolves the review reference to the locked submission, authorizes
--      the reviewer, enforces separation of duties, validates the verified
--      snapshot, writes payload.verified, flips draft -> confirmed, invokes
--      exactly one allowlisted Phase 2 posting RPC via a fixed IF/ELSIF
--      chain (no dynamic SQL, no caller-controlled function names), records
--      the audit row, marks the case decided, and queues a safe WhatsApp
--      acknowledgement to the ORIGINAL report sender. Any error rolls
--      everything back together.
--   6. public.amose_reject_submission(...): atomic draft -> rejected with a
--      required reason, audit history, and a safe acknowledgement. Never
--      creates operational or Brain records.
--
-- Human authorization model (explicit, normalized):
--   - The reviewer is identified ONLY by (provider, provider_sender) resolved
--     through public.biz_sender_identities -- the repository's verified
--     sender identity system. Unknown/unmatched senders fail closed.
--   - The linked employee must have active = true in public.biz_employees.
--   - The employee must hold an ACTIVE public.biz_review_authorizations row
--     for the submission's (tenant_id, business_id) scope with
--     can_confirm / can_reject for the attempted action. A row whose
--     branch_id IS NULL is explicitly business-wide and matches any branch
--     of that business; a row with a branch matches that branch exactly.
--   - service_role possession or ordinary biz_assignments membership alone
--     never authorizes: both RPCs resolve the human reviewer from the
--     sender identity plus the authorization table on every call, and
--     caller-supplied tenant/business/branch arguments do not exist (scope
--     is read from the locked submission row only).
--
-- Separation of duties (enforced in PostgreSQL, not only in Python):
--   - The employee who submitted/reported a submission
--     (biz_submissions.employee_id) can never confirm or reject that same
--     submission. The confirm/reject RPCs reject self-review with the
--     generic UNAUTHORIZED message, and the BEFORE INSERT trigger
--     public._amose_guard_review_audit_no_self_review() on
--     public.biz_review_audit rejects any direct audit insert that would
--     record a reviewer as the reviewer of their own submission.
--
-- Review references:
--   - Format YR-XXXXXXXXXX over the unambiguous uppercase alphabet
--     ABCDEFGHJKMNPQRSTUVWXYZ23456789 (no 0/O, 1/I/L). Generated from
--     gen_random_uuid() randomness, encoded base-31 inside the database,
--     with a retry loop on the UNIQUE(review_ref) constraint, so references
--     are opaque, non-sequential, non-secret, and collision-safe. Never
--     derived from the submission UUID; database IDs are never exposed.
--   - UNIQUE(review_ref) globally; UNIQUE(tenant_id, request_key) for
--     tenant-scoped issue idempotency; UNIQUE(tenant, business, branch,
--     submission) so a draft holds exactly one reference. Terminal
--     submissions cannot be issued a reference or reopened. Identical
--     issue requests are idempotent; a reused key on another submission,
--     or a second reference for one submission, fails closed.
--   - Cross-tenant guessing reveals nothing: the reference is resolved
--     inside the reviewer's own tenant (taken from their verified sender
--     identity), so another tenant's reference reads exactly like a
--     nonexistent one (NOT_FOUND either way).
--
-- Authoritative acknowledgement routing:
--   - No caller-supplied provider_account or destination is accepted (the
--     old p_provider_account arguments are gone). Routing is derived from
--     trusted data only: the destination is the original report sender
--     (the submission employee's sender identity, preferring the review
--     channel), and the provider account is the snapshot stored on the
--     original inbound message row (biz_message_inbox.provider_account).
--   - Both must be known BEFORE any posting/state change; otherwise the
--     RPC fails with ROUTING before confirming/rejecting, so an
--     acknowledgement that is guaranteed to fail is never queued.
--
-- State machine (biz_submissions.status is CHECK-constrained by migration
-- 001 to draft / confirmed / rejected -- there is no pending/voided state):
--   - reviewable: draft only.
--   - amose_confirm_submission: draft -> confirmed (action 'confirmed', or
--     'corrected' when the reviewer attaches a correction reason).
--   - amose_reject_submission: draft -> rejected (reason required).
--   - confirmed rows can never be rejected or re-verified; rejected rows can
--     never be confirmed. A retry with the identical tenant-scoped request
--     key returns the original complete result; the same key with different
--     data fails closed. Keys never collide across tenants.
--
-- Outbound change (why): public.biz_outbound_messages.related_task_id was
-- NOT NULL with a FK to biz_tasks, but a review acknowledgement is not
-- caused by a task. This migration relaxes the column to nullable and adds
-- a guard CHECK so that ONLY the two review acknowledgement types may have
-- a NULL task -- every other message type still requires one. Existing rows
-- already satisfy the CHECK. Acknowledgements are QUEUED only (status
-- queued; never sent here). Ack texts carry no UUIDs, secrets, references
-- beyond the opaque review reference, or payload details.
--
-- WhatsApp wiring status: the parser (human_confirmation.parse_review_command)
-- accepts ONLY explicit commands carrying a YR- review reference, never a
-- UUID; casual words ("yes", "ok", "confirm") never parse. Inbound wiring
-- stays disconnected from message_processor and the webhook until a safe
-- integration point exists; ordinary inbound reports still create drafts
-- only. The command/service/database boundary itself is now internally
-- usable without exposing UUIDs (issue -> REVIEW command -> confirm/reject
-- by reference).
--
-- Security model: RLS enabled on every new table with no policies (fail
-- closed); ALL revoked from anon/authenticated/service_role first, then
-- service_role gets SELECT + INSERT only on the append-only audit table
-- (no UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN). The
-- authorization and case tables grant NOTHING to any caller role: the RPCs
-- read/write them through SECURITY DEFINER (owner) rights only. The audit
-- and case tables reuse public.reject_history_rewrite() plus dedicated
-- guard triggers so history cannot be rewritten and references stay
-- immutable for every role, including the table owner. Internal helpers are
-- owner-only. Public workflow RPCs are executable by service_role only.
-- All SECURITY DEFINER functions use a fixed search_path (pg_catalog) and
-- schema-qualified references. Phase 2 posting RPC privileges are untouched.
begin;

-- ---------------------------------------------------------------------------
-- 0. Drop the superseded Phase 3 draft signatures if a previous version of
-- this migration was ever applied (fresh resets never hit these; the DROP
-- keeps development databases that applied the older draft overloads clean).
-- The old UUID-based, globally-idempotent, caller-routed overloads must not
-- survive alongside the corrected reference-based RPCs.
-- ---------------------------------------------------------------------------
drop function if exists
  public.amose_confirm_submission(uuid, text, text, jsonb, text, text, text);
drop function if exists
  public.amose_reject_submission(uuid, text, text, text, text, text);

-- ---------------------------------------------------------------------------
-- 1. Explicit reviewer authorization. The ONLY source of review authority.
-- Tenant/business scope is mandatory; branch scope is optional, where NULL
-- branch explicitly means business-wide. Employee key types mirror the
-- existing schema exactly (tenant uuid, business/branch text, employee uuid).
-- ---------------------------------------------------------------------------
create table public.biz_review_authorizations (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text,
  employee_id uuid not null,
  can_confirm boolean not null default true,
  can_reject boolean not null default true,
  active boolean not null default true,
  created_at timestamptz not null default pg_catalog.now(),
  updated_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  -- Null-tolerant composite FK: a NULL branch_id skips enforcement (exactly
  -- the business-wide semantics); a non-NULL branch must be a real branch
  -- of this tenant/business.
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, employee_id)
    references public.biz_employees(tenant_id, id),
  -- A branch-scoped authorization always belongs to a business.
  check (business_id is not null or branch_id is null),
  -- An authorization that can do nothing is a configuration error.
  check (can_confirm or can_reject)
);
comment on table public.biz_review_authorizations is
  'Explicit human review authority. NULL branch_id means business-wide; otherwise the branch must match exactly. No rows are seeded.';
-- At most one ACTIVE authorization per (tenant, business, branch-or-wide,
-- employee) scope. coalesce() makes NULL (business-wide) participate in the
-- uniqueness; inactive rows are history and never match.
create unique index biz_review_authorizations_active_scope_unique
  on public.biz_review_authorizations
    (tenant_id, business_id, coalesce(branch_id, ''), employee_id)
  where active = true;
create index biz_review_authorizations_employee
  on public.biz_review_authorizations(tenant_id, employee_id)
  where active = true;

-- ---------------------------------------------------------------------------
-- 2. Review cases: one opaque, immutable, non-secret reference per
-- reviewable draft submission. The reference is the ONLY submission handle
-- ever exposed to WhatsApp reviewers.
-- ---------------------------------------------------------------------------
create table public.biz_review_cases (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  submission_id uuid not null,
  -- Opaque reference, format YR-XXXXXXXXXX. Immutable (guard trigger),
  -- globally unique, never derived from the submission UUID.
  review_ref text not null,
  -- Tenant-scoped issue idempotency key supplied by the caller.
  request_key text not null,
  status text not null default 'open'
    check (status in ('open', 'confirmed', 'rejected')),
  issued_at timestamptz not null default pg_catalog.now(),
  decided_at timestamptz,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, business_id, branch_id, submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  check (review_ref ~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$'),
  check ((status = 'open' and decided_at is null)
    or (status in ('confirmed', 'rejected') and decided_at is not null)),
  unique (review_ref),
  unique (tenant_id, request_key),
  -- Exactly one case per submission: a draft can obtain precisely one
  -- reference, never duplicate cases.
  unique (tenant_id, business_id, branch_id, submission_id)
);
comment on table public.biz_review_cases is
  'Opaque review references (YR-XXXXXXXXXX) mapped 1:1 to draft submissions. Resolved to the locked submission inside the RPCs.';
create index biz_review_cases_submission
  on public.biz_review_cases(tenant_id, business_id, branch_id, submission_id);

-- ---------------------------------------------------------------------------
-- 3. Append-only review/audit history. Composite FKs everywhere: a row can
-- never point at another tenant's (or another business/branch's)
-- submission, nor at another tenant's employee. Idempotency is
-- tenant-scoped: UNIQUE (tenant_id, request_key) so one tenant can never
-- reserve or conflict with another tenant's key.
-- ---------------------------------------------------------------------------
create table public.biz_review_audit (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  submission_id uuid not null,
  -- The opaque reference this review was issued under (traceability; the
  -- reference itself stays immutable on the case row).
  review_ref text not null,
  action text not null check (action in ('corrected', 'confirmed', 'rejected')),
  reviewer_employee_id uuid not null,
  reviewed_at timestamptz not null default pg_catalog.now(),
  -- Rejections and corrections must explain themselves; plain confirmations
  -- carry no reason (kept NULL so the three actions stay unambiguous).
  reason text,
  -- The exact verified block applied by this review (confirm/correct only).
  verified_snapshot jsonb,
  -- Posting type dispatched by the confirm transaction (confirm/correct).
  posting_type text
    check (posting_type is null
      or posting_type in ('production', 'sale', 'payment', 'expense')),
  -- The complete structured result returned to the caller (IDs + status),
  -- so an identical retry can return the original result without re-posting.
  result jsonb,
  -- Deterministic idempotency key supplied by the caller; unique per tenant
  -- so a key can never be recycled across submissions within a tenant, yet
  -- never collides with another tenant's key.
  request_key text not null,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, business_id, branch_id, submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  foreign key (tenant_id, reviewer_employee_id)
    references public.biz_employees(tenant_id, id),
  unique (tenant_id, request_key),
  check ((action = 'rejected'
      and reason is not null and btrim(reason) <> '')
    or (action = 'corrected'
      and reason is not null and btrim(reason) <> '')
    or (action = 'confirmed' and reason is null)),
  check ((action in ('corrected', 'confirmed')
      and verified_snapshot is not null
      and jsonb_typeof(verified_snapshot) = 'object'
      and posting_type is not null and result is not null)
    or (action = 'rejected'
      and verified_snapshot is null
      and posting_type is null and result is not null))
);
comment on table public.biz_review_audit is
  'Append-only human review history. One row per review action; corrections are new history, never overwrites. Idempotency is tenant-scoped.';
create index biz_review_audit_submission
  on public.biz_review_audit(tenant_id, business_id, branch_id, submission_id, reviewed_at);

-- ---------------------------------------------------------------------------
-- 4. Outbound queue: allow task-less review acknowledgements, nothing else.
-- ---------------------------------------------------------------------------
alter table public.biz_outbound_messages
  alter column related_task_id drop not null;
alter table public.biz_outbound_messages
  add constraint biz_outbound_messages_review_task_check
  check ((message_type in ('review_confirmed', 'review_rejected'))
    = (related_task_id is null));

-- ---------------------------------------------------------------------------
-- 5. Internal helper: mint one opaque review reference. Randomness comes
-- from gen_random_uuid(); 13 hex digits (52 bits, sign-masked to 50) are
-- encoded base-31 over the unambiguous alphabet, giving 10 characters.
-- Collision safety comes from UNIQUE(review_ref) plus the insert retry loop
-- in amose_issue_review_reference, not from randomness alone. Owner-only;
-- never granted to any caller role.
-- ---------------------------------------------------------------------------
create or replace function public._amose_new_review_ref()
returns text
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_alphabet text := 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  v_hex text;
  v_num bigint;
  v_out text := '';
  v_i int;
begin
  v_hex := substr(replace(pg_catalog.gen_random_uuid()::text, '-', ''), 1, 13);
  v_num := abs((('x' || v_hex)::bit(52))::bigint);
  for v_i in 1..10 loop
    v_out := v_out || substr(v_alphabet, (v_num % 31)::int + 1, 1);
    v_num := v_num / 31;
  end loop;
  return 'YR-' || v_out;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 6. Internal helper: resolve the reviewer's verified identity to its
-- tenant and employee. Unknown/unmatched/inactive senders fail closed with
-- the generic UNAUTHORIZED message. Owner-only; never granted.
-- ---------------------------------------------------------------------------
create or replace function public._amose_resolve_reviewer_identity(
  p_provider text,
  p_sender text,
  out o_tenant_id uuid,
  out o_employee_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_sender text;
begin
  if p_provider is null or p_provider not in ('whatsapp', 'telegram') then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
  v_sender := btrim(p_sender);
  if v_sender is null or v_sender = '' then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
  -- Verified identity: exactly one sender-identity row whose employee is
  -- still active. The tenant comes from this trusted row, never from the
  -- caller, so a reference is always resolved inside the reviewer's own
  -- tenant and cross-tenant guesses read as NOT_FOUND.
  select i.tenant_id, i.employee_id into o_tenant_id, o_employee_id
    from public.biz_sender_identities i
    join public.biz_employees e
      on e.tenant_id = i.tenant_id and e.id = i.employee_id
    where i.provider = p_provider
      and i.provider_sender = v_sender
      and e.active = true;
  if not found or o_tenant_id is null or o_employee_id is null then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 7. Internal helper: authorize the reviewer for one action on one scope.
-- Requires an ACTIVE biz_review_authorizations row for the exact
-- (tenant, business) with the needed capability, where a NULL branch row
-- is explicitly business-wide and a branched row must match exactly.
-- Enforces separation of duties: the submitter can never review their own
-- submission. Every failure raises the same generic UNAUTHORIZED message so
-- callers cannot distinguish "unknown sender" from "wrong scope" from
-- "self-review". Owner-only; never granted to any caller role.
-- ---------------------------------------------------------------------------
create or replace function public._amose_authorize_reviewer(
  p_employee_id uuid,
  p_tenant_id uuid,
  p_business_id text,
  p_branch_id text,
  p_submitter_id uuid,
  p_need text
)
returns void
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  if p_need is null or p_need not in ('confirm', 'reject') then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
  -- Separation of duties: the reporter can never review their own report.
  if p_employee_id = p_submitter_id then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
  -- Explicit authorization only: active employee, active authorization row,
  -- capability flag, and scope match (NULL branch = business-wide).
  if not exists (select 1
      from public.biz_employees e
      join public.biz_review_authorizations a
        on a.tenant_id = e.tenant_id
        and a.employee_id = e.id
      where e.tenant_id = p_tenant_id
        and e.id = p_employee_id
        and e.active = true
        and a.active = true
        and a.business_id = p_business_id
        and (a.branch_id is null or a.branch_id = p_branch_id)
        and ((p_need = 'confirm' and a.can_confirm)
          or (p_need = 'reject' and a.can_reject))) then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 8. Internal helper: resolve authoritative acknowledgement routing from
-- trusted data only. The destination is the ORIGINAL report sender (the
-- submission employee's sender identity, preferring the review channel);
-- the provider account is the snapshot on the ORIGINAL inbound message row.
-- Anything missing fails closed with ROUTING before any state change, so an
-- acknowledgement that is guaranteed to fail is never queued. Owner-only.
-- ---------------------------------------------------------------------------
create or replace function public._amose_resolve_ack_routing(
  p_tenant_id uuid,
  p_submitter_id uuid,
  p_reviewer_provider text,
  p_inbox_id uuid,
  out o_provider text,
  out o_sender text,
  out o_account text
)
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  -- Destination: the original report sender's verified identity. Prefer the
  -- same channel the review arrived on; otherwise any verified identity of
  -- the submitter. Never a caller-supplied destination.
  select i.provider, i.provider_sender into o_provider, o_sender
    from public.biz_sender_identities i
    where i.tenant_id = p_tenant_id
      and i.employee_id = p_submitter_id
    order by (i.provider = btrim(p_reviewer_provider)) desc,
      i.provider_sender
    limit 1;
  if o_provider is null or o_sender is null then
    raise exception 'ROUTING: original report sender has no verified sender identity';
  end if;
  -- Provider account: the snapshot from the original inbound message.
  -- A submission with no inbox (or an inbox without an account snapshot)
  -- has no authoritative routing and must fail before any state change.
  if p_inbox_id is null then
    raise exception 'ROUTING: submission has no inbound message for acknowledgement routing';
  end if;
  select nullif(btrim(i.provider_account), '') into o_account
    from public.biz_message_inbox i
    where i.id = p_inbox_id;
  if o_account is null then
    raise exception 'ROUTING: inbound message has no provider account snapshot';
  end if;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 9. Internal helper: validate the reviewer-supplied verified snapshot
-- against the explicit schema for its posting type. Structural only (types,
-- required fields, enums, arithmetic); scope/existence checks stay with the
-- Phase 2 posting RPCs, which re-validate authoritatively inside the same
-- transaction. Owner-only; never granted to any caller role.
-- ---------------------------------------------------------------------------
create or replace function public._amose_validate_verified(
  p_kind text,
  p_posting_type text,
  p_verified jsonb
)
returns void
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_lines jsonb;
  v_line jsonb;
  v_idx int := 0;
  v_tmp_uuid uuid;
  v_tmp_int int;
  v_tmp_bigint bigint;
  v_subtotal bigint := 0;
  v_discount bigint := 0;
  v_total bigint;
  v_pay jsonb;
  v_text text;
begin
  if p_verified is null or jsonb_typeof(p_verified) <> 'object' then
    raise exception 'MALFORMED: verified block must be an object';
  end if;
  -- Boundary: a kind label inside verified, when present, must agree with
  -- the submission kind (same rule as the Phase 2 RPCs).
  if p_verified ? 'kind' and nullif(p_verified ->> 'kind', '') is not null
      and (p_verified ->> 'kind') is distinct from p_kind then
    raise exception 'MALFORMED: verified kind % does not match submission kind %',
      p_verified ->> 'kind', p_kind;
  end if;

  if p_posting_type = 'production' then
    begin
      v_tmp_uuid := nullif(p_verified ->> 'product_id', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires product_id';
    end;
    if v_tmp_uuid is null then
      raise exception 'MALFORMED: verified block requires product_id';
    end if;
    begin
      v_tmp_int := (p_verified ->> 'good_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires good_quantity >= 0';
    end;
    if v_tmp_int is null or v_tmp_int < 0 then
      raise exception 'MALFORMED: verified block requires good_quantity >= 0';
    end if;
    if p_verified ? 'rejected_quantity' then
      begin
        v_tmp_int := (p_verified ->> 'rejected_quantity')::int;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block requires rejected_quantity >= 0';
      end;
      if v_tmp_int is null or v_tmp_int < 0 then
        raise exception 'MALFORMED: verified block requires rejected_quantity >= 0';
      end if;
    end if;
    begin
      if (p_verified ->> 'production_date')::date is null then
        raise exception 'MALFORMED: verified block requires production_date';
      end if;
    exception when invalid_datetime_format then
      raise exception 'MALFORMED: verified block requires production_date YYYY-MM-DD';
    end;
    v_text := p_verified ->> 'shift';
    if v_text is null or v_text not in ('morning', 'afternoon', 'night', 'full_day') then
      raise exception 'MALFORMED: verified block requires a valid shift';
    end if;
    if p_verified ? 'produced_by' and nullif(p_verified ->> 'produced_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'produced_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid produced_by';
      end;
    end if;

  elsif p_posting_type = 'sale' then
    v_lines := p_verified -> 'lines';
    if v_lines is null or jsonb_typeof(v_lines) <> 'array'
        or jsonb_array_length(v_lines) < 1 then
      raise exception 'MALFORMED: verified block requires at least one sale line';
    end if;
    if p_verified ? 'customer_id' and nullif(p_verified ->> 'customer_id', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'customer_id')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid customer_id';
      end;
    end if;
    if p_verified ? 'discount_kobo' then
      begin
        v_discount := (p_verified ->> 'discount_kobo')::bigint;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid discount_kobo';
      end;
      if v_discount is null or v_discount < 0 then
        raise exception 'MALFORMED: verified block requires discount_kobo >= 0';
      end if;
    end if;
    if p_verified ? 'sold_at' and nullif(p_verified ->> 'sold_at', '') is not null then
      begin
        perform (p_verified ->> 'sold_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified block has invalid sold_at';
      end;
    end if;
    for v_line in select value from jsonb_array_elements(v_lines) as value loop
      v_idx := v_idx + 1;
      if jsonb_typeof(v_line) <> 'object' then
        raise exception 'MALFORMED: sale line % is not an object', v_idx;
      end if;
      begin
        v_tmp_uuid := nullif(v_line ->> 'product_id', '')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: sale line % requires product_id', v_idx;
      end;
      if v_tmp_uuid is null then
        raise exception 'MALFORMED: sale line % requires product_id', v_idx;
      end if;
      begin
        v_tmp_int := (v_line ->> 'quantity')::int;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: sale line % requires quantity > 0', v_idx;
      end;
      if v_tmp_int is null or v_tmp_int <= 0 then
        raise exception 'MALFORMED: sale line % requires quantity > 0', v_idx;
      end if;
      begin
        v_tmp_bigint := (v_line ->> 'unit_price_kobo')::bigint;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: sale line % requires unit_price_kobo >= 0', v_idx;
      end;
      if v_tmp_bigint is null or v_tmp_bigint < 0 then
        raise exception 'MALFORMED: sale line % requires unit_price_kobo >= 0', v_idx;
      end if;
      v_text := coalesce(nullif(v_line ->> 'storage_state', ''), 'normal');
      if v_text not in ('normal', 'cold') then
        raise exception 'MALFORMED: sale line % has invalid storage_state', v_idx;
      end if;
      v_subtotal := v_subtotal + (v_tmp_int::bigint * v_tmp_bigint);
    end loop;
    if v_discount > v_subtotal then
      raise exception 'MALFORMED: discount % exceeds subtotal %', v_discount, v_subtotal;
    end if;
    v_total := v_subtotal - v_discount;
    v_pay := p_verified -> 'payment';
    if v_pay is not null then
      if jsonb_typeof(v_pay) <> 'object' then
        raise exception 'MALFORMED: verified payment block is not an object';
      end if;
      begin
        v_tmp_bigint := (v_pay ->> 'amount_kobo')::bigint;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified payment block requires amount_kobo > 0';
      end;
      if v_tmp_bigint is null or v_tmp_bigint <= 0 then
        raise exception 'MALFORMED: verified payment block requires amount_kobo > 0';
      end if;
      v_text := v_pay ->> 'method';
      if v_text is null or v_text not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
        raise exception 'MALFORMED: verified payment block requires a valid method';
      end if;
      if v_tmp_bigint > v_total then
        raise exception 'MALFORMED: embedded payment % exceeds sale total %', v_tmp_bigint, v_total;
      end if;
      if v_pay ? 'received_by' and nullif(v_pay ->> 'received_by', '') is not null then
        begin
          v_tmp_uuid := (v_pay ->> 'received_by')::uuid;
        exception when invalid_text_representation then
          raise exception 'MALFORMED: verified payment block has invalid received_by';
        end;
      end if;
      if v_pay ? 'paid_at' and nullif(v_pay ->> 'paid_at', '') is not null then
        begin
          perform (v_pay ->> 'paid_at')::timestamptz;
        exception when others then
          raise exception 'MALFORMED: verified payment block has invalid paid_at';
        end;
      end if;
    end if;

  elsif p_posting_type = 'payment' then
    begin
      v_tmp_uuid := nullif(p_verified ->> 'sale_id', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires sale_id';
    end;
    if v_tmp_uuid is null then
      raise exception 'MALFORMED: verified block requires sale_id';
    end if;
    begin
      v_tmp_bigint := (p_verified ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end;
    if v_tmp_bigint is null or v_tmp_bigint <= 0 then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end if;
    v_text := p_verified ->> 'method';
    if v_text is null or v_text not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
      raise exception 'MALFORMED: verified block requires a valid method';
    end if;
    if p_verified ? 'received_by' and nullif(p_verified ->> 'received_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'received_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid received_by';
      end;
    end if;
    if p_verified ? 'paid_at' and nullif(p_verified ->> 'paid_at', '') is not null then
      begin
        perform (p_verified ->> 'paid_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified payment block has invalid paid_at';
      end;
    end if;

  elsif p_posting_type = 'expense' then
    v_text := p_verified ->> 'category';
    if v_text is null or v_text not in ('fuel', 'maintenance', 'salaries',
        'transport', 'packaging', 'utilities', 'rent', 'purchases', 'other') then
      raise exception 'MALFORMED: verified block requires a valid category';
    end if;
    if nullif(btrim(p_verified ->> 'description'), '') is null then
      raise exception 'MALFORMED: verified block requires a non-empty description';
    end if;
    begin
      v_tmp_bigint := (p_verified ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end;
    if v_tmp_bigint is null or v_tmp_bigint <= 0 then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end if;
    v_text := coalesce(nullif(p_verified ->> 'payment_method', ''), 'cash');
    if v_text not in ('cash', 'transfer', 'pos', 'other') then
      raise exception 'MALFORMED: verified block has invalid payment_method';
    end if;
    if p_verified ? 'incurred_at' and nullif(p_verified ->> 'incurred_at', '') is not null then
      begin
        perform (p_verified ->> 'incurred_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified block has invalid incurred_at';
      end;
    end if;
    if p_verified ? 'paid_by' and nullif(p_verified ->> 'paid_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'paid_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid paid_by';
      end;
    end if;
    if p_verified ? 'recorded_by' and nullif(p_verified ->> 'recorded_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'recorded_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid recorded_by';
      end;
    end if;
    if p_verified ? 'approval_request_id'
        and nullif(p_verified ->> 'approval_request_id', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'approval_request_id')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid approval_request_id';
      end;
    end if;

  else
    raise exception 'UNSUPPORTED_KIND: no verified schema for posting type %', p_posting_type;
  end if;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 10. Trigger guards (PostgreSQL-level enforcement, defense in depth behind
-- the RPC checks): separation of duties on the audit table, and immutability
-- of review references on the case table. Owner-only helpers, never granted.
-- ---------------------------------------------------------------------------
create or replace function public._amose_guard_review_audit_no_self_review()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_submitter uuid;
begin
  select s.employee_id into v_submitter
    from public.biz_submissions s
    where s.tenant_id = NEW.tenant_id
      and s.business_id = NEW.business_id
      and s.branch_id = NEW.branch_id
      and s.id = NEW.submission_id;
  if not found or v_submitter is null then
    raise exception 'INCOMPLETE: review audit references an unknown submission';
  end if;
  if NEW.reviewer_employee_id = v_submitter then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
  return NEW;
end;
$func$;

create or replace function public._amose_guard_review_cases_immutable()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  if TG_OP = 'DELETE' then
    raise exception 'INCOMPLETE: review cases cannot be deleted';
  end if;
  -- Only the lifecycle columns may change; the reference, scope, submission
  -- binding, and issue key are immutable once written.
  if OLD.tenant_id is distinct from NEW.tenant_id
      or OLD.business_id is distinct from NEW.business_id
      or OLD.branch_id is distinct from NEW.branch_id
      or OLD.submission_id is distinct from NEW.submission_id
      or OLD.review_ref is distinct from NEW.review_ref
      or OLD.request_key is distinct from NEW.request_key then
    raise exception 'INCOMPLETE: review case identity is immutable';
  end if;
  if OLD.status = 'confirmed' or OLD.status = 'rejected' then
    if NEW.status is distinct from OLD.status then
      raise exception 'NOT_REVIEWABLE: a decided review case cannot be reopened';
    end if;
  end if;
  return NEW;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 11. Issue (or idempotently re-return) the single review reference for a
-- draft submission. Terminal submissions cannot receive a reference and
-- cannot be reopened. Identical requests (same tenant-scoped key) return
-- the stored mapping; a reused key on another submission, or a second
-- reference for one submission, fails closed.
-- ---------------------------------------------------------------------------
create or replace function public.amose_issue_review_reference(
  p_submission_id uuid,
  p_request_key text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid;
  v_kind text; v_status text;
  v_key text;
  v_ref text;
  v_case_status text;
  v_attempts int := 0;
begin
  if p_submission_id is null then
    raise exception 'NOT_FOUND: submission id is required';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;

  -- Lock first so concurrent issues serialize on this row.
  select s.tenant_id, s.business_id, s.branch_id, s.id, s.kind, s.status
    into v_tenant, v_business, v_branch, v_sub, v_kind, v_status
    from public.biz_submissions s
    where s.id = p_submission_id
    for update;
  if not found then
    raise exception 'NOT_FOUND: submission % does not exist', p_submission_id;
  end if;

  -- Identical retry (tenant-scoped): same key, same submission returns the
  -- stored mapping without creating anything.
  select c.review_ref, c.status into v_ref, v_case_status
    from public.biz_review_cases c
    where c.tenant_id = v_tenant
      and c.request_key = v_key;
  if found then
    if not exists (select 1 from public.biz_review_cases c
        where c.tenant_id = v_tenant
          and c.request_key = v_key
          and c.business_id = v_business
          and c.branch_id = v_branch
          and c.submission_id = v_sub) then
      raise exception 'CONFLICT: request key was already used for a different submission';
    end if;
    return jsonb_build_object('status', v_case_status,
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'is_retry', true);
  end if;

  -- One reference per submission: a second key for the same draft fails.
  if exists (select 1 from public.biz_review_cases c
      where c.tenant_id = v_tenant
        and c.business_id = v_business
        and c.branch_id = v_branch
        and c.submission_id = v_sub) then
    raise exception 'CONFLICT: submission % already has a review reference', v_sub;
  end if;

  -- Terminal submissions can never receive a reference or be reopened.
  if v_status is distinct from 'draft' then
    raise exception 'NOT_REVIEWABLE: submission % has status %, only draft submissions can receive a review reference',
      v_sub, v_status;
  end if;

  -- Mint the reference; retry on the (astronomically unlikely) collision.
  loop
    v_attempts := v_attempts + 1;
    v_ref := public._amose_new_review_ref();
    begin
      insert into public.biz_review_cases
        (tenant_id, business_id, branch_id, submission_id,
         review_ref, request_key)
        values (v_tenant, v_business, v_branch, v_sub,
          v_ref, v_key);
      exit;
    exception when unique_violation then
      -- A concurrent identical insert already stored this submission's
      -- mapping: return it instead of failing.
      select c.review_ref, c.status into v_ref, v_case_status
        from public.biz_review_cases c
        where c.tenant_id = v_tenant
          and c.business_id = v_business
          and c.branch_id = v_branch
          and c.submission_id = v_sub;
      if found then
        return jsonb_build_object('status', v_case_status,
          'review_ref', v_ref,
          'submission_id', v_sub::text,
          'submission_kind', v_kind,
          'request_key', v_key,
          'is_retry', true);
      end if;
      if v_attempts >= 8 then
        raise exception 'INCOMPLETE: could not mint a unique review reference';
      end if;
      -- Otherwise the collision was on review_ref itself: mint again.
    end;
  end loop;

  return jsonb_build_object('status', 'open',
    'review_ref', v_ref,
    'submission_id', v_sub::text,
    'submission_kind', v_kind,
    'request_key', v_key,
    'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 12. Atomic confirm-and-post by review reference. One call = one
-- transaction: resolve the reference inside the reviewer's own tenant, lock
-- the submission, authorize (explicit authorization + separation of
-- duties), tenant-scoped idempotency, validate, resolve authoritative ack
-- routing BEFORE any state change, write verified, confirm, post (exactly
-- one Phase 2 RPC via the fixed branch below), audit, mark the case
-- decided, queue acknowledgement to the original sender. Any error rolls
-- back confirmation, posting, Brain memory, audit, case, and outbound
-- together. No caller routing arguments exist.
-- ---------------------------------------------------------------------------
create or replace function public.amose_confirm_submission(
  p_review_ref text,
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_verified jsonb,
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
  v_kind text; v_status text; v_payload jsonb; v_inbox uuid;
  v_case_id uuid; v_case_status text; v_ref text;
  v_posting text;
  v_key text; v_reason text;
  v_action text;
  v_audit_id uuid;
  v_post jsonb; v_out jsonb; v_existing jsonb;
  v_ack_provider text; v_ack_sender text; v_acct text;
begin
  v_ref := nullif(btrim(p_review_ref), '');
  if v_ref is null or v_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  if p_verified is null or jsonb_typeof(p_verified) <> 'object' then
    raise exception 'MALFORMED: verified block must be an object';
  end if;
  v_reason := nullif(btrim(p_correction_reason), '');
  if v_reason is not null and pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: correction reason is too long';
  end if;

  -- Reviewer identity first: the tenant comes from this trusted row, so the
  -- reference below resolves inside the reviewer's own tenant and a
  -- cross-tenant guess reads exactly like a nonexistent reference.
  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, p_reviewer_sender) as o;

  -- Resolve the reference to its case and lock it, then lock the submission
  -- so concurrent confirmations serialize on this row.
  select c.id, c.business_id, c.branch_id, c.submission_id, c.status
    into v_case_id, v_business, v_branch, v_sub, v_case_status
    from public.biz_review_cases c
    where c.tenant_id = v_tenant
      and c.review_ref = v_ref
    for update;
  if not found then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.payload, s.inbox_id
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_payload, v_inbox
    from public.biz_submissions s
    where s.tenant_id = v_tenant
      and s.business_id = v_business
      and s.branch_id = v_branch
      and s.id = v_sub
    for update;
  if not found then
    raise exception 'INCOMPLETE: review case % has no submission', v_ref;
  end if;

  -- Human authorization: explicit active authorization plus separation of
  -- duties. Checked BEFORE the idempotent retry is returned, so retries
  -- also resolve the locked submission and the reviewer scope first.
  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_business, v_branch, v_submitter, 'confirm');

  -- Identical retry (checked AFTER the lock so a concurrent first attempt
  -- is visible): same tenant-scoped key, same reference, same verified
  -- data, and a consistent confirmed status returns the original result.
  select a.result into v_existing
    from public.biz_review_audit a
    where a.tenant_id = v_tenant
      and a.request_key = v_key;
  if found then
    if (v_existing ->> 'submission_id') is distinct from v_sub::text
        or (v_existing ->> 'review_ref') is distinct from v_ref
        or ((v_existing ->> 'review_action') in ('confirmed', 'corrected')) is not true then
      raise exception 'CONFLICT: request key was already used for a different review';
    end if;
    if (v_existing -> 'verified_snapshot') is distinct from p_verified then
      raise exception 'CONFLICT: request key was already used with different verified data';
    end if;
    if v_status is distinct from 'confirmed' then
      raise exception 'INCOMPLETE: submission % has a confirmation record but status %', v_sub, v_status;
    end if;
    return (v_existing || jsonb_build_object('is_retry', true));
  end if;

  if v_status is distinct from 'draft' then
    raise exception 'NOT_REVIEWABLE: submission % has status %, only draft submissions can be confirmed',
      v_sub, v_status;
  end if;

  -- Fixed kind -> posting-type mapping (mirrors operational_posting.py).
  -- No caller input selects the posting function.
  if v_kind = 'production' or v_kind = 'poultry_daily_report' then
    v_posting := 'production';
  elsif v_kind = 'sale' then
    v_posting := 'sale';
  elsif v_kind = 'payment' then
    v_posting := 'payment';
  elsif v_kind = 'expense' then
    v_posting := 'expense';
  else
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, which has no posting mapping',
      v_sub, v_kind;
  end if;

  perform public._amose_validate_verified(v_kind, v_posting, p_verified);

  -- Authoritative acknowledgement routing BEFORE any posting/state change:
  -- the destination is the original report sender and the provider account
  -- is the original inbound snapshot. If either is unavailable, fail here.
  select o.o_provider, o.o_sender, o.o_account
    into v_ack_provider, v_ack_sender, v_acct
    from public._amose_resolve_ack_routing(
      v_tenant, v_submitter, p_reviewer_provider, v_inbox) as o;

  -- Write ONLY the verified block; every other payload key (raw message,
  -- parsed facts) is left untouched.
  update public.biz_submissions s
    set payload = jsonb_set(coalesce(s.payload, '{}'::jsonb),
        '{verified}', p_verified),
      status = 'confirmed'
    where s.id = v_sub;

  -- Exactly one Phase 2 posting call, chosen by the fixed branch above.
  if v_posting = 'production' then
    v_post := public.amose_post_production(v_sub);
  elsif v_posting = 'sale' then
    v_post := public.amose_post_sale(v_sub);
  elsif v_posting = 'payment' then
    v_post := public.amose_post_payment(v_sub);
  elsif v_posting = 'expense' then
    v_post := public.amose_post_expense(v_sub);
  else
    raise exception 'UNSUPPORTED_KIND: no posting function for type %', v_posting;
  end if;

  v_action := case when v_reason is null then 'confirmed' else 'corrected' end;
  v_audit_id := pg_catalog.gen_random_uuid();
  v_out := (v_post - 'status' - 'is_retry')
    || jsonb_build_object('status', 'confirmed',
      'review_action', v_action,
      'submission_kind', v_kind,
      'submission_id', v_sub::text,
      'review_ref', v_ref,
      'request_key', v_key,
      'audit_id', v_audit_id::text,
      'verified_snapshot', p_verified,
      'is_retry', false);

  insert into public.biz_review_audit
    (id, tenant_id, business_id, branch_id, submission_id, review_ref, action,
     reviewer_employee_id, reason, verified_snapshot, posting_type,
     result, request_key)
    values (v_audit_id, v_tenant, v_business, v_branch, v_sub, v_ref, v_action,
      v_reviewer, v_reason, p_verified, v_posting,
      v_out, v_key);

  update public.biz_review_cases c
    set status = 'confirmed', decided_at = pg_catalog.now()
    where c.id = v_case_id;

  -- Safe acknowledgement through the durable queue only (queued, never
  -- sent here). No UUIDs, secrets, or payload details in the text. The
  -- destination is the original report sender and the provider account is
  -- the original inbound snapshot -- both resolved authoritatively above.
  -- The idempotency key is tenant-scoped so tenants never collide.
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id,
     provider, provider_sender, provider_account,
     related_task_id, message_type, message_text, status, idempotency_key)
    values (v_tenant, v_business, v_branch, v_submitter,
      v_ack_provider, v_ack_sender, v_acct,
      null, 'review_confirmed',
      'Review confirmed: ' || v_posting || ' report for '
        || v_business || '/' || v_branch || ' has been posted. Ref ' || v_ref || '.',
      'queued', 'review_ack:' || v_tenant::text || ':' || v_key);

  return v_out;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 13. Atomic rejection by review reference. Locks, authorizes (explicit
-- authorization + separation of duties), requires a reason, resolves
-- authoritative ack routing BEFORE the state change, flips draft ->
-- rejected, records immutable audit history, marks the case decided, and
-- queues a safe acknowledgement to the original sender. Creates no
-- operational or Brain records of any kind.
-- ---------------------------------------------------------------------------
create or replace function public.amose_reject_submission(
  p_review_ref text,
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_reason text,
  p_request_key text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_reviewer uuid;
  v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text; v_inbox uuid;
  v_case_id uuid; v_case_status text; v_ref text;
  v_key text; v_reason text;
  v_audit_id uuid;
  v_out jsonb; v_existing jsonb;
  v_ack_provider text; v_ack_sender text; v_acct text;
begin
  v_ref := nullif(btrim(p_review_ref), '');
  if v_ref is null or v_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_reason := nullif(btrim(p_reason), '');
  if v_reason is null then
    raise exception 'MALFORMED: rejection requires a non-blank reason';
  end if;
  if pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: rejection reason is too long';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, p_reviewer_sender) as o;

  select c.id, c.business_id, c.branch_id, c.submission_id, c.status
    into v_case_id, v_business, v_branch, v_sub, v_case_status
    from public.biz_review_cases c
    where c.tenant_id = v_tenant
      and c.review_ref = v_ref
    for update;
  if not found then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.inbox_id
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_inbox
    from public.biz_submissions s
    where s.tenant_id = v_tenant
      and s.business_id = v_business
      and s.branch_id = v_branch
      and s.id = v_sub
    for update;
  if not found then
    raise exception 'INCOMPLETE: review case % has no submission', v_ref;
  end if;

  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_business, v_branch, v_submitter, 'reject');

  -- Identical retry returns the original result; anything else fails.
  select a.result into v_existing
    from public.biz_review_audit a
    where a.tenant_id = v_tenant
      and a.request_key = v_key;
  if found then
    if (v_existing ->> 'submission_id') is distinct from v_sub::text
        or (v_existing ->> 'review_ref') is distinct from v_ref
        or ((v_existing ->> 'review_action') = 'rejected') is not true then
      raise exception 'CONFLICT: request key was already used for a different review';
    end if;
    if (v_existing ->> 'reason') is distinct from v_reason then
      raise exception 'CONFLICT: request key was already used with a different reason';
    end if;
    if v_status is distinct from 'rejected' then
      raise exception 'INCOMPLETE: submission % has a rejection record but status %', v_sub, v_status;
    end if;
    return (v_existing || jsonb_build_object('is_retry', true));
  end if;

  if v_status is distinct from 'draft' then
    raise exception 'NOT_REVIEWABLE: submission % has status %, only draft submissions can be rejected',
      v_sub, v_status;
  end if;

  -- Authoritative acknowledgement routing BEFORE the state change.
  select o.o_provider, o.o_sender, o.o_account
    into v_ack_provider, v_ack_sender, v_acct
    from public._amose_resolve_ack_routing(
      v_tenant, v_submitter, p_reviewer_provider, v_inbox) as o;

  update public.biz_submissions s
    set status = 'rejected'
    where s.id = v_sub;

  v_audit_id := pg_catalog.gen_random_uuid();
  v_out := jsonb_build_object('status', 'rejected',
    'review_action', 'rejected',
    'submission_kind', v_kind,
    'submission_id', v_sub::text,
    'review_ref', v_ref,
    'request_key', v_key,
    'reason', v_reason,
    'audit_id', v_audit_id::text,
    'is_retry', false);

  insert into public.biz_review_audit
    (id, tenant_id, business_id, branch_id, submission_id, review_ref, action,
     reviewer_employee_id, reason, verified_snapshot, posting_type,
     result, request_key)
    values (v_audit_id, v_tenant, v_business, v_branch, v_sub, v_ref, 'rejected',
      v_reviewer, v_reason, null, null,
      v_out, v_key);

  update public.biz_review_cases c
    set status = 'rejected', decided_at = pg_catalog.now()
    where c.id = v_case_id;

  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id,
     provider, provider_sender, provider_account,
     related_task_id, message_type, message_text, status, idempotency_key)
    values (v_tenant, v_business, v_branch, v_submitter,
      v_ack_provider, v_ack_sender, v_acct,
      null, 'review_rejected',
      'Review update: ' || v_kind || ' report for '
        || v_business || '/' || v_branch || ' was rejected. Ref ' || v_ref || ': '
        || pg_catalog.left(v_reason, 200),
      'queued', 'review_ack:' || v_tenant::text || ':' || v_key);

  return v_out;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 14. RLS + least privilege. Fail closed for every caller role. ALL is
-- revoked from PUBLIC, anon, authenticated, and service_role first.
--
-- Privilege matrix (this migration):
--   biz_review_authorizations: RLS ON, no policies;
--     PUBLIC/anon/authenticated/service_role: NO privileges (not even
--     SELECT). Reads/writes happen only inside SECURITY DEFINER RPCs.
--   biz_review_cases: RLS ON, no policies;
--     PUBLIC/anon/authenticated/service_role: NO privileges. Resolved only
--     inside SECURITY DEFINER RPCs. No safe list view is exposed in this
--     phase, so no restricted RPC is needed either.
--   biz_review_audit: RLS ON, no policies;
--     PUBLIC/anon/authenticated: NO privileges;
--     service_role: SELECT + INSERT only
--       (no UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN).
--   Internal helpers (_amose_new_review_ref, _amose_resolve_reviewer_identity,
--     _amose_authorize_reviewer, _amose_resolve_ack_routing,
--     _amose_validate_verified, _amose_guard_review_audit_no_self_review,
--     _amose_guard_review_cases_immutable): EXECUTE revoked from
--     PUBLIC/anon/authenticated/service_role (owner-only).
--   Public workflow RPCs (amose_issue_review_reference,
--     amose_confirm_submission, amose_reject_submission): EXECUTE revoked
--     from PUBLIC/anon/authenticated; granted to service_role only.
--
-- The rewrite-guard trigger aborts UPDATE/DELETE on the audit table for
-- every role, including the table owner; the self-review guard aborts
-- separation-of-duties violations; the case-immutability guard aborts
-- reference rewrites, deletes, and reopening. Phase 2 posting RPC
-- privileges are untouched.
-- ---------------------------------------------------------------------------
alter table public.biz_review_authorizations enable row level security;
alter table public.biz_review_cases enable row level security;
alter table public.biz_review_audit enable row level security;

revoke all on public.biz_review_authorizations
  from public, anon, authenticated, service_role;
revoke all on public.biz_review_cases
  from public, anon, authenticated, service_role;
revoke all on public.biz_review_audit
  from public, anon, authenticated, service_role;

grant select, insert on public.biz_review_audit to service_role;

revoke truncate on public.biz_review_authorizations from service_role;
revoke truncate on public.biz_review_cases from service_role;
revoke truncate on public.biz_review_audit from service_role;

create trigger biz_review_audit_no_rewrite
  before update or delete on public.biz_review_audit
  for each row execute function public.reject_history_rewrite();

create trigger biz_review_audit_no_self_review
  before insert on public.biz_review_audit
  for each row execute function public._amose_guard_review_audit_no_self_review();

create trigger biz_review_cases_immutable
  before update or delete on public.biz_review_cases
  for each row execute function public._amose_guard_review_cases_immutable();

-- Internal helpers: owner-only. The SECURITY DEFINER workflow RPCs call
-- them internally (definer rights, ownership chain), which needs no caller
-- grant.
revoke execute on function public._amose_new_review_ref()
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_resolve_reviewer_identity(text, text)
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_authorize_reviewer(uuid, uuid, text, text, uuid, text)
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_resolve_ack_routing(uuid, uuid, text, uuid)
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_validate_verified(text, text, jsonb)
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_guard_review_audit_no_self_review()
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_guard_review_cases_immutable()
  from public, anon, authenticated, service_role;

-- Public workflow RPCs: service_role only. Note the confirm signature
-- (five required args plus one trailing default) and the reject/issue
-- signatures.
revoke execute on function public.amose_issue_review_reference(uuid, text)
  from public, anon, authenticated;
revoke execute on function public.amose_confirm_submission(text, text, text, jsonb, text, text)
  from public, anon, authenticated;
revoke execute on function public.amose_reject_submission(text, text, text, text, text)
  from public, anon, authenticated;

grant execute on function public.amose_issue_review_reference(uuid, text)
  to service_role;
grant execute on function public.amose_confirm_submission(text, text, text, jsonb, text, text)
  to service_role;
grant execute on function public.amose_reject_submission(text, text, text, text, text)
  to service_role;

commit;
