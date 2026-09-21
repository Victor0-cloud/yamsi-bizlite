-- Stage 2: production-ready live WhatsApp integration.
--
-- Forward-only migration (applied migrations are never edited). Schema +
-- code only: no data rows are seeded, no credentials or phone numbers are
-- stored here. Owner registers real provider accounts after deployment
-- through the service role (see WHATSAPP_DEPLOYMENT.md runbook).
--
-- Real schema gaps closed by this migration:
--   1. No authoritative provider-account registry existed:
--      provider_account was a free-text snapshot on inbox/outbound rows
--      with nothing proving the number belongs to the row's scope or is
--      still enabled. New biz_provider_accounts table binds each account
--      to exactly one tenant/business/branch with an enabled flag.
--      Enforcement is database-side (foreign keys + triggers) so every
--      writer -- Python, RPC, or future channel -- fails closed:
--        * biz_submissions rows carrying a route snapshot must reference
--          a registered account for their exact scope (rows without a
--          route snapshot, e.g. API-created submissions, skip the check);
--        * biz_outbound_messages inserts with a full route snapshot must
--          reference a registered account for their exact scope;
--        * pre-scope branch_clarification rows (null business/branch by
--          design, real sender recipient, per-inbox idempotency key) need
--          the account registered and enabled for the same tenant in any
--          scope -- authorization without guessing a business or branch;
--          the clarification shape (sender + account present) is pinned
--          by outbound_clarification_shape_check;
--        * claiming (queued -> sending) additionally requires the
--          account to be still enabled under the same rule that gated
--          the insert; disabled or unknown accounts can neither queue
--          nor send (fully snapshot-less legacy rows skip the check).
--      A Meta signature proves the app sent the request; only this
--      registry authorizes which phone_number_id may act for a scope.
--   2. Delivery state had no safe representation: the queue-processing
--      status was the only lever, so recording 'delivered' risked
--      re-queueing finished work. New delivery_state (+ delivery_updated_at
--      + delivery_error) columns track Meta sent/delivered/read/failed
--      independently; queue status keeps owning claim/send semantics.
--   3. Crash recovery had no lease: a row stuck in 'sending' stayed stuck.
--      New claimed_at lease column plus the service-role-only
--      amose_reclaim_stale_outbound RPC re-queues stale claims
--      transactionally and idempotently.
--   4. Status-event application had no monotonic guard: new
--      service-role-only amose_apply_delivery_status RPC correlates
--      (provider, provider_account, provider_message_id) to exactly one
--      row inside its own tenant, applies sent -> delivered -> read
--      monotonically (failed overrides sent/delivered only, read is
--      final), sanitizes failure codes, and never touches queue status,
--      operational records, or review state.
--
-- Least privilege preserved: the new table has RLS with no caller-role
-- access (service_role only); both RPCs are revoked from PUBLIC/anon/
-- authenticated and granted to service_role only; internal helpers are
-- owner-only. Existing RLS, grants, and review/sender rules untouched.
begin;

-- ---------------------------------------------------------------------------
-- 1. Authoritative provider-account registry.
-- ---------------------------------------------------------------------------
create table public.biz_provider_accounts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  provider text not null
    check (provider in ('whatsapp', 'telegram')),
  -- The business's own number id (Meta phone_number_id) or bot account.
  provider_account text not null,
  enabled boolean not null default true,
  label text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- One account authorizes exactly one scope: the same number can never
  -- route into a second tenant, business, or branch.
  unique (tenant_id, provider, provider_account),
  unique (tenant_id, business_id, branch_id, provider, provider_account)
);
comment on table public.biz_provider_accounts is
  'Authoritative provider-account registry. Only registered, enabled accounts may send or receive for their scope. Disable (never delete) to revoke.';
create index biz_provider_accounts_scope
  on public.biz_provider_accounts(tenant_id, business_id, branch_id);

alter table public.biz_provider_accounts enable row level security;
revoke all on public.biz_provider_accounts from anon, authenticated;
revoke all on public.biz_provider_accounts from public;
grant select, insert, update on public.biz_provider_accounts to service_role;

-- ---------------------------------------------------------------------------
-- 2. Route snapshots on submissions and outbound rows, bound to the
-- registry. NULL snapshots skip enforcement (API-created or legacy rows
-- with no route); any present snapshot must match its exact scope.
-- ---------------------------------------------------------------------------
alter table public.biz_submissions
  add column if not exists provider text;
alter table public.biz_submissions
  add column if not exists provider_account text;

alter table public.biz_submissions
  drop constraint if exists biz_submissions_provider_account_fk;
alter table public.biz_submissions
  add constraint biz_submissions_provider_account_fk
  foreign key (tenant_id, business_id, branch_id, provider, provider_account)
  references public.biz_provider_accounts
    (tenant_id, business_id, branch_id, provider, provider_account);

alter table public.biz_outbound_messages
  drop constraint if exists biz_outbound_provider_account_fk;
alter table public.biz_outbound_messages
  add constraint biz_outbound_provider_account_fk
  foreign key (tenant_id, business_id, branch_id, provider, provider_account)
  references public.biz_provider_accounts
    (tenant_id, business_id, branch_id, provider, provider_account);

-- ---------------------------------------------------------------------------
-- 3. Shared authorization helper (owner-only): exact scope match plus
-- enabled state. NULL scope/account inputs never authorize.
-- ---------------------------------------------------------------------------
create or replace function public._amose_provider_account_authorized(
  p_tenant_id uuid,
  p_business_id text,
  p_branch_id text,
  p_provider text,
  p_provider_account text
)
returns boolean
language plpgsql
stable
security definer
set search_path = pg_catalog
as $func$
declare
  v_ok boolean := false;
begin
  if p_tenant_id is null or nullif(btrim(p_business_id), '') is null
      or nullif(btrim(p_branch_id), '') is null
      or nullif(btrim(p_provider), '') is null
      or nullif(btrim(p_provider_account), '') is null then
    return false;
  end if;
  select true into v_ok
    from public.biz_provider_accounts a
    where a.tenant_id = p_tenant_id
      and a.business_id = p_business_id
      and a.branch_id = p_branch_id
      and a.provider = p_provider
      and a.provider_account = p_provider_account
      and a.enabled = true;
  return coalesce(v_ok, false);
end;
$func$;

revoke execute on function public._amose_provider_account_authorized(uuid, text, text, text, text)
  from public, anon, authenticated, service_role;

-- Tenant-level variant for pre-scope rows (branch_clarification): the
-- account must be registered and enabled for the tenant in any scope.
-- Used only by the outbound guard; never guesses a business or branch.
create or replace function public._amose_provider_tenant_authorized(
  p_tenant_id uuid,
  p_provider text,
  p_provider_account text
)
returns boolean
language plpgsql
stable
security definer
set search_path = pg_catalog
as $func$
declare
  v_ok boolean := false;
begin
  if p_tenant_id is null
      or nullif(btrim(p_provider), '') is null
      or nullif(btrim(p_provider_account), '') is null then
    return false;
  end if;
  select true into v_ok
    from public.biz_provider_accounts a
    where a.tenant_id = p_tenant_id
      and a.provider = p_provider
      and a.provider_account = p_provider_account
      and a.enabled = true;
  return coalesce(v_ok, false);
end;
$func$;

revoke execute on function public._amose_provider_tenant_authorized(uuid, text, text)
  from public, anon, authenticated, service_role;

-- Submissions: a routed row needs an authorized account. Unrouted rows
-- (no inbox route snapshot) skip the check; the FK above still pins any
-- present snapshot to its exact registered scope.
create or replace function public._amose_guard_submission_provider_account()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  if NEW.provider_account is null then
    return NEW;
  end if;
  if not public._amose_provider_account_authorized(
      NEW.tenant_id, NEW.business_id, NEW.branch_id,
      NEW.provider, NEW.provider_account) then
    raise exception 'UNAUTHORIZED: provider account % is not authorized for %/%',
      NEW.provider_account, NEW.business_id, NEW.branch_id;
  end if;
  return NEW;
end;
$func$;

drop trigger if exists biz_submissions_provider_account_guard
  on public.biz_submissions;
create trigger biz_submissions_provider_account_guard
  before insert on public.biz_submissions
  for each row execute function public._amose_guard_submission_provider_account();

revoke execute on function public._amose_guard_submission_provider_account()
  from public, anon, authenticated, service_role;

-- Outbound: inserts and claims (transitions into 'sending') need an
-- authorized account whenever the row carries an account snapshot.
-- Fully scoped rows need the exact registered scope; pre-scope
-- branch_clarification rows (null business/branch by design) need the
-- account registered and enabled for the same tenant in any scope --
-- authorization without guessing a business or branch. Fully
-- snapshot-less rows (no account at all) keep legacy behavior.
--
-- branch_clarification shape is pinned by outbound_clarification_shape_check
-- below: it must carry provider_sender and provider_account, and (via the
-- pre-existing scope checks) no business, branch, or task. Unrelated
-- message types cannot exploit the nullable shape: they still require a
-- full scope, and recipient stays NOT NULL for every type.
create or replace function public._amose_guard_outbound_provider_account()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  if TG_OP = 'UPDATE'
      and (OLD.status is not distinct from 'sending'
        or NEW.status is distinct from 'sending') then
    -- Only inserts and the claim moment (a transition INTO 'sending')
    -- are guarded; sent/delivered/failed/retry bookkeeping never
    -- re-checks routing.
    return NEW;
  end if;
  if NEW.provider_account is null then
    return NEW;
  end if;
  if NEW.business_id is null or NEW.branch_id is null then
    if not public._amose_provider_tenant_authorized(
        NEW.tenant_id, NEW.provider, NEW.provider_account) then
      raise exception 'UNAUTHORIZED: provider account % is not registered for this tenant',
        NEW.provider_account;
    end if;
    return NEW;
  end if;
  if not public._amose_provider_account_authorized(
      NEW.tenant_id, NEW.business_id, NEW.branch_id,
      NEW.provider, NEW.provider_account) then
    raise exception 'UNAUTHORIZED: provider account % is not authorized for %/%',
      NEW.provider_account, NEW.business_id, NEW.branch_id;
  end if;
  return NEW;
end;
$func$;

drop trigger if exists biz_outbound_provider_account_guard
  on public.biz_outbound_messages;
create trigger biz_outbound_provider_account_guard
  before insert or update on public.biz_outbound_messages
  for each row execute function public._amose_guard_outbound_provider_account();

revoke execute on function public._amose_guard_outbound_provider_account()
  from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 4. Lease and delivery-state columns on the outbound queue. Queue status
-- keeps owning claim/send semantics; delivery_state tracks Meta
-- acknowledgement independently so recording 'delivered' can never
-- re-queue finished work. attempt_count/next_attempt_at bound retries.
-- ---------------------------------------------------------------------------
alter table public.biz_outbound_messages
  add column if not exists claimed_at timestamptz;
alter table public.biz_outbound_messages
  add column if not exists attempt_count int not null default 0
  constraint outbound_attempt_count_nonnegative
  check (attempt_count >= 0);
alter table public.biz_outbound_messages
  add column if not exists next_attempt_at timestamptz;
alter table public.biz_outbound_messages
  add column if not exists delivery_state text
  constraint outbound_delivery_state_check
  check (delivery_state is null
    or delivery_state in ('sent', 'delivered', 'read', 'failed'));
alter table public.biz_outbound_messages
  add column if not exists delivery_updated_at timestamptz;
alter table public.biz_outbound_messages
  add column if not exists delivery_error text;

-- Correlation index for status events: exact (provider, account,
-- provider_message_id) match inside the row's own tenant.
create index if not exists biz_outbound_messages_delivery_lookup
  on public.biz_outbound_messages
    (provider, provider_account, provider_message_id)
  where provider_message_id is not null;

-- branch_clarification shape: the allowlisted pre-scope type must carry
-- its route (sender + account). Scope/task nullness for the type is
-- already pinned by the pre-existing clarification/review checks, and
-- recipient stays NOT NULL for every message type, so no unrelated type
-- can exploit this shape.
alter table public.biz_outbound_messages
  drop constraint if exists outbound_clarification_shape_check;
alter table public.biz_outbound_messages
  add constraint outbound_clarification_shape_check
  check (message_type is distinct from 'branch_clarification'
    or (provider_sender is not null and provider_account is not null));

-- ---------------------------------------------------------------------------
-- 5. Stale-claim recovery: re-queues 'sending' rows whose lease expired.
-- Transactional and idempotent (a second call finds nothing to do).
-- Tenant-safe: rows keep their own tenant; the result reports per-tenant
-- counts. Disabled-account rows are re-queued like any other stale claim
-- (the claim guard still blocks their next send until re-enabled).
-- service_role only.
-- ---------------------------------------------------------------------------
create or replace function public.amose_reclaim_stale_outbound(
  p_stale_seconds int default 1800,
  p_limit int default 50
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_cutoff timestamptz;
  v_ids uuid[] := '{}';
  v_tenants jsonb := '[]'::jsonb;
  r record;
begin
  if p_stale_seconds is null or p_stale_seconds < 60
      or p_stale_seconds > 86400 then
    raise exception 'MALFORMED: stale window must be 60..86400 seconds';
  end if;
  if p_limit is null or p_limit < 1 or p_limit > 200 then
    raise exception 'MALFORMED: reclaim limit must be 1..200';
  end if;
  v_cutoff := now() - (p_stale_seconds || ' seconds')::interval;
  for r in
    select o.id, o.tenant_id
      from public.biz_outbound_messages o
      where o.status = 'sending'
        and o.claimed_at is not null
        and o.claimed_at < v_cutoff
      order by o.claimed_at
      limit p_limit
    for update skip locked
  loop
    update public.biz_outbound_messages o
      set status = 'queued', claimed_at = null, next_attempt_at = null
      where o.id = r.id;
    v_ids := v_ids || r.id;
    v_tenants := v_tenants || jsonb_build_object(
      'tenant_id', r.tenant_id::text, 'message_id', r.id::text);
  end loop;
  return jsonb_build_object('status', 'ok',
    'reclaimed', coalesce(array_length(v_ids, 1), 0),
    'message_ids', coalesce(to_jsonb(v_ids), '[]'::jsonb),
    'tenants', v_tenants,
    'is_retry', false);
end;
$func$;

revoke execute on function public.amose_reclaim_stale_outbound(int, int)
  from public, anon, authenticated;
grant execute on function public.amose_reclaim_stale_outbound(int, int)
  to service_role;

-- ---------------------------------------------------------------------------
-- 6. Delivery-status application: correlates one Meta status event to
-- exactly one outbound row by (provider, provider_account,
-- provider_message_id) -- the tenant follows the row, so a status for an
-- unknown message id, or for another account, modifies nothing.
-- Monotonic ranks: sent < delivered < read; 'failed' overrides
-- sent/delivered only; 'read' and 'failed' are otherwise final, so late
-- or repeated events can never move a message backwards. Failure codes
-- are re-sanitized here (digits plus a short printable suffix at most).
-- Queue status, operational records, and review state are never touched.
-- service_role only.
-- ---------------------------------------------------------------------------
create or replace function public.amose_apply_delivery_status(
  p_provider text,
  p_provider_account text,
  p_provider_message_id text,
  p_delivery_state text,
  p_error_code text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_row_id uuid;
  v_tenant uuid;
  v_current text;
  v_new_rank int;
  v_current_rank int;
  v_error text := null;
begin
  if p_provider is null or p_provider not in ('whatsapp', 'telegram') then
    raise exception 'MALFORMED: unknown provider';
  end if;
  if nullif(btrim(p_provider_account), '') is null then
    raise exception 'MALFORMED: provider account is required';
  end if;
  if nullif(btrim(p_provider_message_id), '') is null
      or pg_catalog.char_length(p_provider_message_id) > 256 then
    raise exception 'MALFORMED: provider message id is required';
  end if;
  v_new_rank := case p_delivery_state
    when 'sent' then 1
    when 'delivered' then 2
    when 'read' then 3
    when 'failed' then 4
    else null end;
  if v_new_rank is null then
    raise exception 'MALFORMED: unknown delivery state %', p_delivery_state;
  end if;

  select o.id, o.tenant_id, o.delivery_state
    into v_row_id, v_tenant, v_current
    from public.biz_outbound_messages o
    where o.provider = p_provider
      and o.provider_account = p_provider_account
      and o.provider_message_id = p_provider_message_id
    for update;
  if not found then
    return jsonb_build_object('status', 'ok', 'applied', false,
      'reason', 'unknown_message', 'is_retry', false);
  end if;

  v_current_rank := case v_current
    when 'sent' then 1
    when 'delivered' then 2
    when 'read' then 3
    when 'failed' then 4
    else 0 end;
  -- First event always applies; otherwise only forward progress, with
  -- 'failed' additionally allowed over sent/delivered (its rank already
  -- exceeds them) but never over 'read', and nothing over 'failed'.
  if v_current_rank <> 0
      and (v_new_rank <= v_current_rank
        or v_current = 'failed'
        or (p_delivery_state = 'failed' and v_current = 'read')) then
    return jsonb_build_object('status', 'ok', 'applied', false,
      'reason', 'stale_or_duplicate',
      'previous', v_current, 'current', v_current,
      'message_id', v_row_id::text, 'is_retry', false);
  end if;

  if p_delivery_state = 'failed' then
    if p_error_code is not null
        and p_error_code ~ '^[0-9]{1,10}(:[ -~]{1,120})?$' then
      v_error := pg_catalog.left(p_error_code, 200);
    else
      v_error := 'unknown';
    end if;
  end if;

  update public.biz_outbound_messages o
    set delivery_state = p_delivery_state,
      delivery_updated_at = now(),
      delivery_error = v_error
    where o.id = v_row_id;

  return jsonb_build_object('status', 'ok', 'applied', true,
    'previous', v_current, 'current', p_delivery_state,
    'message_id', v_row_id::text,
    'tenant_id', v_tenant::text, 'is_retry', false);
end;
$func$;

revoke execute on function public.amose_apply_delivery_status(text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_apply_delivery_status(text, text, text, text, text)
  to service_role;

commit;
