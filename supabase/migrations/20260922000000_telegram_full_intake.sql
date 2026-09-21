-- Telegram full intake, end to end.
--
-- Forward-only migration (applied migrations are never edited). Schema +
-- code only: no data rows are seeded and no business-policy VALUES are
-- inserted. Money stays integer kobo (bigint); floats are never used.
--
-- What changes and why:
--   1. Three new operational tables for the intake kinds that had no
--      posting mapping (STOCK, CUSTOMER PAYMENT, CUSTOMER DEBT):
--        public.biz_stock_counts      (branch stock snapshot by storage
--          state, resolved to one scoped product at confirmation)
--        public.biz_customer_debts    (customer owes the business; open /
--          settled / voided with full audit, like sales)
--        public.biz_customer_payments (customer payment without a prior
--          sale; confirmed + matching cash-custody entry, like payments)
--      Customers are never invented: the confirm/posting path resolves a
--      staff-written customer NAME to exactly one active customer of the
--      same tenant/business (branch-wide or branch-specific), and fails
--      closed when there is none or several. Products resolve the same
--      way (exactly one active product of the business). Employees named
--      in handover/deposit reports resolve against active employees
--      assigned to the submission's business/branch.
--   2. Three new transactional, idempotent, service-role-only posting
--      RPCs following the Phase 2 architecture exactly (single-uuid
--      argument, locked confirmed submission, deterministic
--      'posting:<submission_id>:<role>' keys, retry-completeness checks,
--      verified Brain memory + context links in the same transaction):
--        public.amose_post_stock(uuid)             -> posting_type stock
--        public.amose_post_customer_payment(uuid)  -> posting_type
--          customer_payment
--        public.amose_post_customer_debt(uuid)     -> posting_type
--          customer_debt
--   3. New owner-only helper public._amose_validate_verified_intake()
--      for the three new verified schemas. The existing validator is
--      untouched; amose_confirm_submission dispatches to one or the
--      other by submission kind.
--   4. amose_confirm_submission learns the kind -> posting-type mapping
--      and the posting call branch for the three new kinds. Every other
--      line (reviewer resolution, authorization, retry handling, ack
--      routing, audit, case lifecycle) is unchanged.
--   5. amose_queue_review_requests and
--      amose_queue_telegram_review_requests accept the three new kinds
--      on both channels (kind gates only; reference issuance,
--      authorization, separation of duties, routing, and idempotency
--      are unchanged).
--   6. _amose_review_request_text covers the three new kinds, and every
--      kind now carries CONFIRM guidance: chat confirm builds the
--      verified block inside the database for every kind, so no kind
--      needs API review anymore.
--   7. amose_review_confirm_command builds the verified block from the
--      submission's own parsed extraction for every kind (sale keeps its
--      exact rules; production/expense/handover/deposit/payment/stock/
--      customer_payment/customer_debt gain deterministic builders with
--      documented safe defaults: rejected 0, full_day shift, cash method
--      for expenses, reporter as fallback depositor/receiver, inbox
--      received date for production dates). Anything unresolvable fails
--      closed with a MALFORMED message naming what to send instead.
--   8. The review-audit posting_type CHECK accepts the three new types.
--
-- Least privilege preserved: new tables have RLS with no caller-role
-- access (service_role gets the same scoped grants as their sibling
-- tables; append-only tables get no UPDATE and no TRUNCATE). New RPCs
-- are revoked from PUBLIC/anon/authenticated and granted to
-- service_role only. Internal helpers stay owner-only (revoked from
-- every role including service_role). CREATE OR REPLACE preserves
-- existing revokes on replaced functions. RLS, sender-resolution, and
-- review-reference rules are untouched.
begin;

-- ---------------------------------------------------------------------------
-- 1. New operational tables. All nullable-side columns keep existing rows
-- unaffected (there are none: these tables are new). Corrections are new
-- rows, never rewrites.
-- ---------------------------------------------------------------------------
create table public.biz_stock_counts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  product_id uuid not null,
  normal_quantity integer not null check (normal_quantity >= 0),
  cold_quantity integer not null check (cold_quantity >= 0),
  counted_at timestamptz not null default now(),
  counted_by uuid,
  recorded_by uuid,
  source_submission_id uuid,
  notes text,
  idempotency_key text not null,
  created_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The product must belong to the count's own tenant and business.
  foreign key (tenant_id, business_id, product_id)
    references public.biz_products(tenant_id, business_id, id),
  foreign key (tenant_id, counted_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, recorded_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so memories pin the exact branch-scoped count.
  unique (tenant_id, business_id, branch_id, id)
);
comment on table public.biz_stock_counts is
  'Branch stock snapshots by storage state. Append-only: corrections are new counts, never rewrites.';
create index biz_stock_counts_branch_time
  on public.biz_stock_counts(tenant_id, business_id, branch_id, counted_at);
create index biz_stock_counts_product
  on public.biz_stock_counts(tenant_id, business_id, product_id);

create table public.biz_customer_debts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  customer_id uuid not null,
  amount_kobo bigint not null check (amount_kobo > 0),
  status text not null default 'open'
    check (status in ('open', 'settled', 'voided')),
  incurred_at timestamptz not null default now(),
  recorded_by uuid,
  source_submission_id uuid,
  idempotency_key text not null,
  settled_by uuid,
  settled_at timestamptz,
  voided_by uuid,
  voided_at timestamptz,
  void_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The customer must belong to the debt's own tenant and business
  -- (branch-wide or branch-specific customers both qualify).
  foreign key (tenant_id, business_id, customer_id)
    references public.biz_customers(tenant_id, business_id, id),
  foreign key (tenant_id, recorded_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, settled_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, voided_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so payments/memories pin the exact branch-scoped debt.
  unique (tenant_id, business_id, branch_id, id),
  -- Open debts carry no audit trail; settled debts need actor AND
  -- timestamp; voided debts need the full void audit (actor, timestamp,
  -- reason).
  check ((status = 'open' and settled_by is null and settled_at is null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'settled' and settled_by is not null
      and settled_at is not null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'voided' and voided_by is not null and voided_at is not null
      and void_reason is not null))
);
comment on table public.biz_customer_debts is
  'Customer debts (customer owes the business). Settling/voiding are audited status changes, never deletes.';
create index biz_customer_debts_branch_status
  on public.biz_customer_debts(tenant_id, business_id, branch_id, status);
create index biz_customer_debts_customer
  on public.biz_customer_debts(customer_id);

create table public.biz_customer_payments (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  customer_id uuid not null,
  amount_kobo bigint not null check (amount_kobo > 0),
  method text not null
    check (method in ('cash', 'transfer', 'pos', 'credit_adjustment')),
  received_by uuid,
  reference text,
  status text not null default 'confirmed'
    check (status in ('confirmed', 'reversed')),
  paid_at timestamptz not null default now(),
  source_submission_id uuid,
  idempotency_key text not null,
  reversed_by uuid,
  reversed_at timestamptz,
  reversal_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The customer must belong to the payment's own tenant and business.
  foreign key (tenant_id, business_id, customer_id)
    references public.biz_customers(tenant_id, business_id, id),
  foreign key (tenant_id, received_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, reversed_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so custody pins the exact branch-scoped payment.
  unique (tenant_id, business_id, branch_id, id),
  -- Confirmed payments carry the receiver; reversed payments need the
  -- full reversal audit (actor, timestamp, reason).
  check ((status = 'confirmed' and reversed_by is null
      and reversed_at is null and reversal_reason is null)
    or (status = 'reversed' and reversed_by is not null
      and reversed_at is not null and reversal_reason is not null))
);
comment on table public.biz_customer_payments is
  'Customer payments without a prior sale. Reversal is an audited status change, never a delete.';
create index biz_customer_payments_branch_time
  on public.biz_customer_payments(tenant_id, business_id, branch_id, paid_at);
create index biz_customer_payments_customer
  on public.biz_customer_payments(customer_id);
create index biz_customer_payments_status
  on public.biz_customer_payments(tenant_id, business_id, branch_id, status);

-- ---------------------------------------------------------------------------
-- 2. Least privilege for the new tables (mirrors the sibling tables:
-- RLS on, nothing for caller roles, scoped service_role grants;
-- append-only stock counts get no UPDATE and no TRUNCATE).
-- ---------------------------------------------------------------------------
alter table public.biz_stock_counts enable row level security;
alter table public.biz_customer_debts enable row level security;
alter table public.biz_customer_payments enable row level security;

revoke all on public.biz_stock_counts,
  public.biz_customer_debts, public.biz_customer_payments
  from anon, authenticated, service_role;

-- Append-only snapshot ledger: no update grant, so posted counts stay
-- immutable.
grant select, insert on public.biz_stock_counts to service_role;
grant select, insert, update on public.biz_customer_debts to service_role;
grant select, insert, update on public.biz_customer_payments to service_role;

revoke truncate on public.biz_stock_counts from service_role;

-- ---------------------------------------------------------------------------
-- 3. Intake resolvers: staff-written NAMES to authoritative scoped UUIDs.
-- Deterministic and fail-closed: exactly one match wins, anything else
-- raises without guessing. Owner-only; never granted to any caller role.
-- ---------------------------------------------------------------------------
create or replace function public._amose_resolve_intake_customer(
  p_tenant uuid,
  p_business text,
  p_branch text,
  p_name text
)
returns uuid
language plpgsql
stable
security definer
set search_path = pg_catalog
as $func$
declare
  v_name text;
  v_count int;
  v_id uuid;
begin
  v_name := nullif(btrim(p_name), '');
  if v_name is null or p_tenant is null
      or nullif(btrim(p_business), '') is null
      or nullif(btrim(p_branch), '') is null then
    raise exception 'MALFORMED: a customer name is required';
  end if;
  -- Business-wide customers (null branch) and this branch's customers
  -- both qualify; anything outside this tenant/business never matches.
  -- (UUIDs have no min() aggregate, so count first, then fetch.)
  select count(*) into v_count
    from public.biz_customers c
    where c.tenant_id = p_tenant
      and c.business_id = p_business
      and (c.branch_id is null or c.branch_id = p_branch)
      and c.is_active = true
      and lower(btrim(c.name)) = lower(v_name);
  if v_count < 1 then
    raise exception 'MALFORMED: no active customer named % exists here; create the customer first, then confirm',
      left(v_name, 40);
  end if;
  if v_count > 1 then
    raise exception 'MALFORMED: several customers are named %; confirm with an exact customer reference instead',
      left(v_name, 40);
  end if;
  select c.id into v_id
    from public.biz_customers c
    where c.tenant_id = p_tenant
      and c.business_id = p_business
      and (c.branch_id is null or c.branch_id = p_branch)
      and c.is_active = true
      and lower(btrim(c.name)) = lower(v_name)
    limit 1;
  return v_id;
end;
$func$;

revoke execute on function public._amose_resolve_intake_customer(uuid, text, text, text)
  from public, anon, authenticated, service_role;

create or replace function public._amose_resolve_intake_employee(
  p_tenant uuid,
  p_business text,
  p_branch text,
  p_name text
)
returns uuid
language plpgsql
stable
security definer
set search_path = pg_catalog
as $func$
declare
  v_name text;
  v_count int;
  v_id uuid;
begin
  v_name := nullif(btrim(p_name), '');
  if v_name is null or p_tenant is null
      or nullif(btrim(p_business), '') is null
      or nullif(btrim(p_branch), '') is null then
    raise exception 'MALFORMED: an employee name is required';
  end if;
  -- Active employees of this tenant assigned to this business/branch
  -- with a matching display name: exactly one wins, never a guess.
  -- (UUIDs have no min() aggregate, so count first, then fetch.)
  select count(*) into v_count
    from public.biz_employees e
    join public.biz_assignments a
      on a.tenant_id = e.tenant_id and a.employee_id = e.id
    where e.tenant_id = p_tenant
      and e.active = true
      and lower(btrim(e.display_name)) = lower(v_name)
      and a.business_id = p_business
      and a.branch_id = p_branch;
  if v_count < 1 then
    raise exception 'MALFORMED: no assigned employee named % exists here',
      left(v_name, 40);
  end if;
  if v_count > 1 then
    raise exception 'MALFORMED: several assigned employees are named %; confirm with an exact employee reference instead',
      left(v_name, 40);
  end if;
  select e.id into v_id
    from public.biz_employees e
    join public.biz_assignments a
      on a.tenant_id = e.tenant_id and a.employee_id = e.id
    where e.tenant_id = p_tenant
      and e.active = true
      and lower(btrim(e.display_name)) = lower(v_name)
      and a.business_id = p_business
      and a.branch_id = p_branch
    limit 1;
  return v_id;
end;
$func$;

revoke execute on function public._amose_resolve_intake_employee(uuid, text, text, text)
  from public, anon, authenticated, service_role;

create or replace function public._amose_resolve_intake_product(
  p_tenant uuid,
  p_business text
)
returns uuid
language plpgsql
stable
security definer
set search_path = pg_catalog
as $func$
declare
  v_count int;
  v_id uuid;
begin
  if p_tenant is null or nullif(btrim(p_business), '') is null then
    raise exception 'MALFORMED: product scope is required';
  end if;
  -- Deterministic default product, never a guess: exactly one active
  -- product in the business wins (same precedent as the chat-confirm
  -- sale unit match). (UUIDs have no min() aggregate, so count first,
  -- then fetch.)
  select count(*) into v_count
    from public.biz_products p
    where p.tenant_id = p_tenant and p.business_id = p_business
      and p.is_active = true;
  if v_count <> 1 then
    raise exception 'MALFORMED: no single active product exists here; confirm with an exact product reference instead';
  end if;
  select p.id into v_id
    from public.biz_products p
    where p.tenant_id = p_tenant and p.business_id = p_business
      and p.is_active = true
    limit 1;
  return v_id;
end;
$func$;

revoke execute on function public._amose_resolve_intake_product(uuid, text)
  from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 4. Verified-block validation for the three new posting types. Shape and
-- range checks only (same split as the existing validator); existence and
-- scope checks stay with the posting RPCs. Owner-only.
-- ---------------------------------------------------------------------------
create or replace function public._amose_validate_verified_intake(
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
  v_tmp_uuid uuid;
  v_tmp_bigint bigint;
  v_tmp_int int;
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

  if p_posting_type = 'stock' then
    begin
      v_tmp_uuid := nullif(p_verified ->> 'product_id', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires product_id';
    end;
    if v_tmp_uuid is null then
      raise exception 'MALFORMED: verified block requires product_id';
    end if;
    begin
      v_tmp_int := (p_verified ->> 'normal_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires normal_quantity >= 0';
    end;
    if v_tmp_int is null or v_tmp_int < 0 then
      raise exception 'MALFORMED: verified block requires normal_quantity >= 0';
    end if;
    begin
      v_tmp_int := (p_verified ->> 'cold_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires cold_quantity >= 0';
    end;
    if v_tmp_int is null or v_tmp_int < 0 then
      raise exception 'MALFORMED: verified block requires cold_quantity >= 0';
    end if;
    if p_verified ? 'counted_at' and nullif(p_verified ->> 'counted_at', '') is not null then
      begin
        perform (p_verified ->> 'counted_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified block has invalid counted_at';
      end;
    end if;
    if p_verified ? 'counted_by' and nullif(p_verified ->> 'counted_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'counted_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid counted_by';
      end;
    end if;
    if p_verified ? 'recorded_by' and nullif(p_verified ->> 'recorded_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'recorded_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid recorded_by';
      end;
    end if;

  elsif p_posting_type = 'customer_payment' then
    -- The customer travels as an authoritative UUID when the reviewer
    -- knows it, or as the staff-written name the posting RPC resolves.
    if nullif(p_verified ->> 'customer_id', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'customer_id')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid customer_id';
      end;
    elsif nullif(btrim(p_verified ->> 'customer_name'), '') is null then
      raise exception 'MALFORMED: verified block requires customer_id or customer_name';
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
        raise exception 'MALFORMED: verified block has invalid paid_at';
      end;
    end if;

  elsif p_posting_type = 'customer_debt' then
    if nullif(p_verified ->> 'customer_id', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'customer_id')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid customer_id';
      end;
    elsif nullif(btrim(p_verified ->> 'customer_name'), '') is null then
      raise exception 'MALFORMED: verified block requires customer_id or customer_name';
    end if;
    begin
      v_tmp_bigint := (p_verified ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end;
    if v_tmp_bigint is null or v_tmp_bigint <= 0 then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end if;
    if p_verified ? 'incurred_at' and nullif(p_verified ->> 'incurred_at', '') is not null then
      begin
        perform (p_verified ->> 'incurred_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified block has invalid incurred_at';
      end;
    end if;
    if p_verified ? 'recorded_by' and nullif(p_verified ->> 'recorded_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'recorded_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid recorded_by';
      end;
    end if;

  else
    raise exception 'UNSUPPORTED_KIND: no verified schema for posting type %', p_posting_type;
  end if;
end;
$func$;

revoke execute on function public._amose_validate_verified_intake(text, text, jsonb)
  from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 5. Stock-count posting: one append-only branch stock snapshot + one
-- verified Brain memory. The product resolves from the authoritative
-- scoped catalogue (exactly one active product, or the verified UUID
-- when the reviewer knew it). All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_stock(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_product uuid; v_normal int; v_cold int;
  v_counted timestamptz; v_counted_by uuid; v_recorded_by uuid;
  v_key_count text;
  v_count_id uuid; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['stock']);

  -- Idempotent retry: the count's deterministic key already exists.
  v_key_count := 'posting:' || v_sub::text || ':stock_count';
  select c.id into v_count_id
    from public.biz_stock_counts c
    where c.tenant_id = v_tenant and c.business_id = v_business
      and c.branch_id = v_branch and c.idempotency_key = v_key_count;
  if found then
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'stock_count';
    -- Retry completeness: count, memory, and count link must exist.
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % stock posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_stock_counts'
          and l.target_record_id = v_count_id::text) then
      raise exception 'INCOMPLETE: submission % stock posting is missing its count context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'stock',
      'submission_id', v_sub::text, 'stock_count_id', v_count_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified stock block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read. A kind label inside
  -- verified, when present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  begin
    v_product := nullif(v_ver ->> 'product_id', '')::uuid;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires product_id', v_sub;
  end;
  if v_product is null then
    raise exception 'MALFORMED: submission % verified block requires product_id', v_sub;
  end if;
  begin
    v_normal := (v_ver ->> 'normal_quantity')::int;
    v_cold := (v_ver ->> 'cold_quantity')::int;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires normal_quantity and cold_quantity >= 0', v_sub;
  end;
  if v_normal is null or v_normal < 0 or v_cold is null or v_cold < 0 then
    raise exception 'MALFORMED: submission % verified block requires normal_quantity and cold_quantity >= 0', v_sub;
  end if;
  v_counted := now();
  if v_ver ? 'counted_at' and nullif(v_ver ->> 'counted_at', '') is not null then
    begin
      v_counted := (v_ver ->> 'counted_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid counted_at', v_sub;
    end;
  end if;
  v_counted_by := v_emp;
  if v_ver ? 'counted_by' and nullif(v_ver ->> 'counted_by', '') is not null then
    begin
      v_counted_by := (v_ver ->> 'counted_by')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid counted_by', v_sub;
    end;
  end if;
  v_recorded_by := v_emp;
  if v_ver ? 'recorded_by' and nullif(v_ver ->> 'recorded_by', '') is not null then
    begin
      v_recorded_by := (v_ver ->> 'recorded_by')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid recorded_by', v_sub;
    end;
  end if;

  -- Scope checks against the authoritative submission scope.
  if not exists (select 1 from public.biz_products p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.id = v_product and p.is_active) then
    raise exception 'SCOPE: product % is not an active product of this tenant/business', v_product;
  end if;
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = v_tenant and e.id = v_counted_by) then
    raise exception 'SCOPE: employee % is not in this tenant', v_counted_by;
  end if;
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = v_tenant and e.id = v_recorded_by) then
    raise exception 'SCOPE: employee % is not in this tenant', v_recorded_by;
  end if;

  insert into public.biz_stock_counts
    (tenant_id, business_id, branch_id, product_id,
     normal_quantity, cold_quantity, counted_at, counted_by, recorded_by,
     source_submission_id, notes, idempotency_key)
  values (v_tenant, v_business, v_branch, v_product,
    v_normal, v_cold, v_counted, v_counted_by, v_recorded_by,
    v_sub, 'confirmed stock count posting', v_key_count)
  returning id into v_count_id;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'stock_count';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'stock_count', v_count_id::text,
      jsonb_build_object('stock_count_id', v_count_id::text,
        'product_id', v_product::text, 'normal_quantity', v_normal,
        'cold_quantity', v_cold),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_stock_counts', v_count_id::text)
  on conflict do nothing;

  return jsonb_build_object('status', 'posted', 'posting_type', 'stock',
    'submission_id', v_sub::text, 'stock_count_id', v_count_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 6. Customer-debt posting: one open biz_customer_debts row + one verified
-- Brain memory. The customer is an authoritative scoped row (UUID when the
-- reviewer knew it, resolved from the staff-written name otherwise).
-- All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_customer_debt(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_customer uuid; v_amount bigint;
  v_incurred timestamptz; v_recorded_by uuid;
  v_key_debt text;
  v_debt_id uuid; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['customer_debt']);

  -- Idempotent retry: the debt's deterministic key already exists.
  v_key_debt := 'posting:' || v_sub::text || ':customer_debt';
  select d.id into v_debt_id
    from public.biz_customer_debts d
    where d.tenant_id = v_tenant and d.business_id = v_business
      and d.branch_id = v_branch and d.idempotency_key = v_key_debt;
  if found then
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'customer_debt';
    -- Retry completeness: debt, memory, and debt link must exist.
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % customer debt posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_customer_debts'
          and l.target_record_id = v_debt_id::text) then
      raise exception 'INCOMPLETE: submission % customer debt posting is missing its debt context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'customer_debt',
      'submission_id', v_sub::text, 'customer_debt_id', v_debt_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified customer debt block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read. A kind label inside
  -- verified, when present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  if nullif(v_ver ->> 'customer_id', '') is not null then
    begin
      v_customer := (v_ver ->> 'customer_id')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % has invalid customer_id', v_sub;
    end;
    if not exists (select 1 from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer and c.is_active = true
          and (c.branch_id is null or c.branch_id = v_branch)) then
      raise exception 'SCOPE: customer % is not an active customer of this tenant/business/branch', v_customer;
    end if;
  else
    v_customer := public._amose_resolve_intake_customer(
      v_tenant, v_business, v_branch, v_ver ->> 'customer_name');
  end if;
  begin
    v_amount := (v_ver ->> 'amount_kobo')::bigint;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end;
  if v_amount is null or v_amount <= 0 then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end if;
  v_incurred := now();
  if v_ver ? 'incurred_at' and nullif(v_ver ->> 'incurred_at', '') is not null then
    begin
      v_incurred := (v_ver ->> 'incurred_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid incurred_at', v_sub;
    end;
  end if;
  v_recorded_by := v_emp;
  if v_ver ? 'recorded_by' and nullif(v_ver ->> 'recorded_by', '') is not null then
    begin
      v_recorded_by := (v_ver ->> 'recorded_by')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid recorded_by', v_sub;
    end;
    if not exists (select 1 from public.biz_employees e
        where e.tenant_id = v_tenant and e.id = v_recorded_by) then
      raise exception 'SCOPE: employee % is not in this tenant', v_recorded_by;
    end if;
  end if;

  insert into public.biz_customer_debts
    (tenant_id, business_id, branch_id, customer_id, amount_kobo,
     status, incurred_at, recorded_by, source_submission_id, idempotency_key)
  values (v_tenant, v_business, v_branch, v_customer, v_amount,
    'open', v_incurred, v_recorded_by, v_sub, v_key_debt)
  returning id into v_debt_id;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'customer_debt';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'customer_debt', v_debt_id::text,
      jsonb_build_object('customer_debt_id', v_debt_id::text,
        'customer_id', v_customer::text, 'amount_kobo', v_amount),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_customer_debts', v_debt_id::text)
  on conflict do nothing;

  return jsonb_build_object('status', 'posted', 'posting_type', 'customer_debt',
    'submission_id', v_sub::text, 'customer_debt_id', v_debt_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 7. Customer-payment posting: one confirmed biz_customer_payments row +
-- its matching append-only cash-custody entry + one verified Brain memory.
-- Mirrors the sale-payment posting (payment + custody + memory), without a
-- prior sale. All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_customer_payment(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_customer uuid; v_amount bigint; v_method text; v_received uuid;
  v_ref text; v_paid_at timestamptz;
  v_key_pay text; v_key_cust text;
  v_pay_id uuid; v_custody_id uuid; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['customer_payment']);

  -- Idempotent retry: the payment's deterministic key already exists.
  v_key_pay := 'posting:' || v_sub::text || ':customer_payment';
  select p.id into v_pay_id
    from public.biz_customer_payments p
    where p.tenant_id = v_tenant and p.business_id = v_business
      and p.branch_id = v_branch and p.idempotency_key = v_key_pay;
  if found then
    v_key_cust := 'posting:' || v_sub::text || ':customer_payment_custody';
    select c.id into v_custody_id
      from public.biz_cash_custody_entries c
      where c.tenant_id = v_tenant and c.business_id = v_business
        and c.branch_id = v_branch and c.idempotency_key = v_key_cust;
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'customer_payment';
    -- Retry completeness: payment, custody, memory and both context
    -- links must exist. Anything less fails closed.
    if v_custody_id is null then
      raise exception 'INCOMPLETE: submission % customer payment posting is missing its cash custody entry', v_sub;
    end if;
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % customer payment posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_customer_payments'
          and l.target_record_id = v_pay_id::text) then
      raise exception 'INCOMPLETE: submission % customer payment posting is missing its payment context link', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_cash_custody_entries'
          and l.target_record_id = v_custody_id::text) then
      raise exception 'INCOMPLETE: submission % customer payment posting is missing its custody context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'customer_payment',
      'submission_id', v_sub::text, 'customer_payment_id', v_pay_id::text,
      'cash_custody_entry_id', v_custody_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified customer payment block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read. A kind label inside
  -- verified, when present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  if nullif(v_ver ->> 'customer_id', '') is not null then
    begin
      v_customer := (v_ver ->> 'customer_id')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % has invalid customer_id', v_sub;
    end;
    if not exists (select 1 from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer and c.is_active = true
          and (c.branch_id is null or c.branch_id = v_branch)) then
      raise exception 'SCOPE: customer % is not an active customer of this tenant/business/branch', v_customer;
    end if;
  else
    v_customer := public._amose_resolve_intake_customer(
      v_tenant, v_business, v_branch, v_ver ->> 'customer_name');
  end if;
  begin
    v_amount := (v_ver ->> 'amount_kobo')::bigint;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end;
  if v_amount is null or v_amount <= 0 then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end if;
  v_method := v_ver ->> 'method';
  if v_method is null or v_method not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
    raise exception 'MALFORMED: submission % verified block requires a valid method', v_sub;
  end if;
  v_received := v_emp;
  if v_ver ? 'received_by' and nullif(v_ver ->> 'received_by', '') is not null then
    begin
      v_received := (v_ver ->> 'received_by')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid received_by', v_sub;
    end;
  end if;
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = v_tenant and e.id = v_received) then
    raise exception 'SCOPE: employee % is not in this tenant', v_received;
  end if;
  v_ref := nullif(v_ver ->> 'reference', '');
  v_paid_at := now();
  if v_ver ? 'paid_at' and nullif(v_ver ->> 'paid_at', '') is not null then
    begin
      v_paid_at := (v_ver ->> 'paid_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid paid_at', v_sub;
    end;
  end if;

  insert into public.biz_customer_payments
    (tenant_id, business_id, branch_id, customer_id, amount_kobo, method,
     received_by, reference, status, paid_at,
     source_submission_id, idempotency_key)
  values (v_tenant, v_business, v_branch, v_customer, v_amount, v_method,
    v_received, v_ref, 'confirmed', v_paid_at,
    v_sub, v_key_pay)
  returning id into v_pay_id;

  v_key_cust := 'posting:' || v_sub::text || ':customer_payment_custody';
  insert into public.biz_cash_custody_entries
    (tenant_id, business_id, branch_id, custodian_id, entry_type,
     amount_kobo, occurred_at, recorded_by,
     notes, idempotency_key)
  values (v_tenant, v_business, v_branch, v_received, 'cash_received',
    v_amount, v_paid_at, v_emp,
    'confirmed customer payment posting', v_key_cust)
  returning id into v_custody_id;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'customer_payment';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'customer_payment', v_pay_id::text,
      jsonb_build_object('customer_payment_id', v_pay_id::text,
        'customer_id', v_customer::text, 'amount_kobo', v_amount,
        'method', v_method),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_customer_payments', v_pay_id::text)
  on conflict do nothing;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_cash_custody_entries', v_custody_id::text)
  on conflict do nothing;

  return jsonb_build_object('status', 'posted', 'posting_type', 'customer_payment',
    'submission_id', v_sub::text, 'customer_payment_id', v_pay_id::text,
    'cash_custody_entry_id', v_custody_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 8. Least privilege for the new posting API: only service_role may
-- execute. Frontend roles fail closed.
-- ---------------------------------------------------------------------------
revoke execute on function public.amose_post_stock(uuid)
  from public, anon, authenticated;
revoke execute on function public.amose_post_customer_debt(uuid)
  from public, anon, authenticated;
revoke execute on function public.amose_post_customer_payment(uuid)
  from public, anon, authenticated;

grant execute on function public.amose_post_stock(uuid) to service_role;
grant execute on function public.amose_post_customer_debt(uuid) to service_role;
grant execute on function public.amose_post_customer_payment(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- 9. Confirmation dispatch learns the three new kinds. Every other line of
-- the confirm transaction (reviewer resolution, authorization, retry
-- handling, ack routing, audit, case lifecycle) is identical to the
-- current version; only the kind -> posting-type mapping, the validator
-- dispatch, and the posting call branch grow.
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
  elsif v_kind = 'cash_handover' then
    v_posting := 'cash_handover';
  elsif v_kind = 'bank_deposit' then
    v_posting := 'bank_deposit';
  elsif v_kind = 'stock' then
    v_posting := 'stock';
  elsif v_kind = 'customer_payment' then
    v_posting := 'customer_payment';
  elsif v_kind = 'customer_debt' then
    v_posting := 'customer_debt';
  else
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, which has no posting mapping',
      v_sub, v_kind;
  end if;

  if v_posting in ('stock', 'customer_payment', 'customer_debt') then
    perform public._amose_validate_verified_intake(v_kind, v_posting, p_verified);
  else
    perform public._amose_validate_verified(v_kind, v_posting, p_verified);
  end if;

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
  elsif v_posting = 'cash_handover' then
    v_post := public.amose_post_cash_handover(v_sub);
  elsif v_posting = 'bank_deposit' then
    v_post := public.amose_post_bank_deposit(v_sub);
  elsif v_posting = 'stock' then
    v_post := public.amose_post_stock(v_sub);
  elsif v_posting = 'customer_payment' then
    v_post := public.amose_post_customer_payment(v_sub);
  elsif v_posting = 'customer_debt' then
    v_post := public.amose_post_customer_debt(v_sub);
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
-- 10. Review-audit posting types accept the three new posting types.
-- ---------------------------------------------------------------------------
alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_posting_type_check;
alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_posting_type_check;
alter table public.biz_review_audit
  add constraint biz_review_audit_posting_type_check
  check (posting_type is null
    or posting_type in ('production', 'sale', 'payment', 'expense',
      'cash_handover', 'bank_deposit',
      'stock', 'customer_payment', 'customer_debt'));

-- ---------------------------------------------------------------------------
-- 11. Review-request queueing accepts the three new kinds on both
-- channels. Bodies are identical transplants of the current queue
-- functions with only the reviewable-kind gate extended; reference
-- issuance, authorization, separation of duties, routing, and
-- idempotency are unchanged. CREATE OR REPLACE preserves the existing
-- service_role-only grants.
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
      'payment', 'expense', 'cash_handover', 'bank_deposit',
      'stock', 'customer_payment', 'customer_debt') then
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

create or replace function public.amose_queue_telegram_review_requests(
  p_submission_id uuid,
  p_request_key text,
  p_bot_account text
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
  v_msg text;
  v_bot text;
  v_notified jsonb := '[]'::jsonb;
  v_notified_count int := 0;
  v_eligible int := 0;
  v_skipped jsonb := '[]'::jsonb;
  v_preexisting boolean := false;
  v_attempts int := 0;
  v_outbound_id uuid;
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
  -- The bot account is server configuration (the single Telegram bot),
  -- never a user-supplied chat value.
  v_bot := nullif(btrim(p_bot_account), '');
  if v_bot is null or pg_catalog.char_length(v_bot) > 128 then
    raise exception 'MALFORMED: bot account is not configured';
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
      'payment', 'expense', 'cash_handover', 'bank_deposit',
      'stock', 'customer_payment', 'customer_debt') then
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, which needs no human review',
      v_sub, v_kind;
  end if;
  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;

  -- Issue (or reuse) the single reference for this draft. Shared with the
  -- WhatsApp queue path: one case per submission, never duplicate cases.
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
            v_ref, 'tgqueue:' || v_key);
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
      'notified', v_notified,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;

  if v_inbox is null then
    return jsonb_build_object('status', 'reviewer_unroutable',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', v_notified,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;

  select exists (select 1 from public.biz_outbound_messages o
      where o.idempotency_key
        like 'tg_review_req:' || v_tenant::text || ':' || v_ref || ':%')
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
        and i.provider = 'telegram'
      order by i.provider_sender
      limit 1;
    if v_sender is null then
      v_skipped := v_skipped || jsonb_build_object(
        'employee_id', r.employee_id::text, 'reason', 'no_telegram_identity');
      continue;
    end if;
    begin
      insert into public.biz_outbound_messages
        (tenant_id, business_id, branch_id, recipient_employee_id,
         provider, provider_sender, provider_account,
         related_task_id, message_type, message_text, status, idempotency_key)
        values (v_tenant, v_business, v_branch, r.employee_id,
          'telegram', v_sender, v_bot,
          null, 'review_request', v_msg,
          'queued', 'tg_review_req:' || v_tenant::text || ':' || v_ref
            || ':' || r.employee_id::text)
        returning id into v_outbound_id;
      v_notified := v_notified || jsonb_build_object(
        'employee_id', r.employee_id::text,
        'provider_sender', v_sender,
        'outbound_id', v_outbound_id::text);
      v_notified_count := v_notified_count + 1;
    exception when unique_violation then
      -- Already notified on an earlier run: not an error, not a duplicate.
      null;
    end;
  end loop;

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

  if v_notified_count > 0 then
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
    'notified', v_notified,
    'skipped', v_skipped,
    'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 12. Review-request text covers the three new kinds, and every kind now
-- carries CONFIRM guidance: chat confirm builds the verified block inside
-- the database for every kind, so no kind needs API review anymore.
-- CREATE OR REPLACE preserves the existing revokes.
-- ---------------------------------------------------------------------------
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
  -- Unresolved fields are reported for every kind (sale keeps its
  -- historical inline wording below; other kinds share this suffix).
  if p_parsed ? 'missing_fields'
      and jsonb_typeof(p_parsed -> 'missing_fields') = 'array'
      and jsonb_array_length(p_parsed -> 'missing_fields') > 0 then
    select 'Missing: ' || pg_catalog.left(
        (select string_agg(value::text, ', ')
          from jsonb_array_elements_text(p_parsed -> 'missing_fields') as value),
        160)
      into v_missing;
  else
    v_missing := null;
  end if;

  if p_kind = 'sale' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'unit_price', '');
    v_money := case when v_raw ~ '^-?[0-9]+(\.[0-9]+)?$'
      then public._amose_format_naira(v_raw::numeric) else null end;
    v_lines := 'Sale proposal: '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'quantity', ''), 24), '?')
      || ' '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'unit', ''), 24), 'units')
      || ' at '
      || coalesce(v_money, '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
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
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'payment' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Payment proposal: amount '
      || coalesce(v_money, '?')
      || ', method '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'method',
        ''), 24), '?')
      || ', sale ref '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'sale_ref',
        ''), 24), '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'expense' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Expense proposal: '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'category',
        ''), 24), '?')
      || ', amount '
      || coalesce(v_money, '?')
      || coalesce(', ' || pg_catalog.left(nullif(v_fields ->> 'description',
        ''), 80), '');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'cash_handover' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Cash handover proposal: amount '
      || coalesce(v_money, '?')
      || ', from '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'from_name',
        ''), 40), '?')
      || ' to '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'to_name',
        ''), 40), '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'bank_deposit' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Bank deposit proposal: amount '
      || coalesce(v_money, '?')
      || ', depositor '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'depositor_name',
        ''), 40), '?')
      || ', destination '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'destination_account',
        ''), 64), '?')
      || ', reference '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'reference',
        ''), 32), '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'stock' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_lines := 'Stock count proposal: normal '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'normal_quantity',
        ''), 24), '?')
      || ', cold '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'cold_quantity',
        ''), 24), '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'customer_payment' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Customer payment proposal: customer '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'customer_name',
        ''), 40), '?')
      || ', amount '
      || coalesce(v_money, '?')
      || ', method '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'method',
        ''), 24), '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  elsif p_kind = 'customer_debt' then
    v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
      then p_parsed -> 'fields' else '{}'::jsonb end;
    v_raw := nullif(v_fields ->> 'amount_kobo', '');
    v_money := case when v_raw ~ '^-?[0-9]+$'
      then public._amose_format_kobo(v_raw::numeric) else null end;
    v_lines := 'Customer debt proposal: customer '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'customer_name',
        ''), 40), '?')
      || ', amount '
      || coalesce(v_money, '?');
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  else
    v_lines := 'Report proposal (' || pg_catalog.left(p_kind, 40) || ')';
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
  end if;

  -- Every kind confirms over chat: the verified block is built inside the
  -- database from the submission's own parsed extraction, so reviewers
  -- use one command shape for all reports.
  v_text := 'Review request ' || p_review_ref || ' for '
    || pg_catalog.left(p_business_id, 64) || '/'
    || pg_catalog.left(p_branch_id, 64) || ': ' || v_lines || '. '
    || 'Reply REVIEW CONFIRM ' || p_review_ref
    || ' KEY your-unique-key to confirm (add CORRECTION reason to correct),'
    || ' or REVIEW REJECT ' || p_review_ref
    || ' KEY your-unique-key REASON why to reject.';
  return v_text;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 13. Chat-confirm path for every kind. Resolves sender identity, the
-- reference (inside the reviewer's own tenant), and explicit confirm
-- authorization plus separation of duties with the Phase 3 helpers;
-- builds the verified block deterministically from the submission's own
-- parsed extraction; then runs the standard Phase 3 confirm
-- transaction. Exactly one write boundary.
--
-- Sale keeps its exact rules. Other kinds gain builders with documented
-- safe defaults (rejected 0, full_day shift, cash expense method,
-- reporter as fallback depositor/receiver, inbox received date for
-- production dates). Anything unresolvable fails closed with a
-- MALFORMED message naming what to send instead.
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
  v_kind text; v_status text; v_payload jsonb; v_inbox uuid;
  v_ref text; v_key text; v_reason text;
  v_parsed jsonb; v_fields jsonb; v_msg_text text;
  v_unit text; v_qty numeric; v_price numeric;
  v_product uuid; v_product_count int;
  v_amount bigint; v_method text; v_text text;
  v_good int; v_rejected int; v_prod_date date; v_shift text;
  v_date_text text; v_received timestamptz;
  v_normal int; v_cold int;
  v_customer uuid; v_customer_id uuid;
  v_employee uuid;
  v_category text; v_desc text;
  v_dest text; v_deposit_ref text; v_depositor uuid;
  v_from uuid; v_to uuid;
  v_sale_ref text; v_sale_sub uuid; v_sale uuid;
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
      s.kind, s.status, s.payload, s.inbox_id
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_payload, v_inbox
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

  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;
  v_fields := case when jsonb_typeof(v_parsed -> 'fields') = 'object'
    then v_parsed -> 'fields' else '{}'::jsonb end;
  v_msg_text := case when jsonb_typeof(v_payload) = 'object'
    then nullif(btrim(v_payload ->> 'message_text'), '') else null end;

  if v_kind = 'sale' then
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

  elsif v_kind = 'production' or v_kind = 'poultry_daily_report' then
    v_product := public._amose_resolve_intake_product(v_tenant, v_business);
    begin
      v_good := (v_fields ->> 'good_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed production has no good quantity';
    end;
    if v_good is null or v_good < 0 or v_good > 2147483647 then
      raise exception 'MALFORMED: parsed production has no good quantity';
    end if;
    v_rejected := 0;
    if nullif(v_fields ->> 'rejected_quantity', '') is not null then
      begin
        v_rejected := (v_fields ->> 'rejected_quantity')::int;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: parsed production has invalid rejected quantity';
      end;
      if v_rejected is null or v_rejected < 0 then
        raise exception 'MALFORMED: parsed production has invalid rejected quantity';
      end if;
    end if;
    -- Production date: staff-stated first, then the inbox received date
    -- (authoritative system fact), then today. Never invented.
    v_prod_date := null;
    v_date_text := nullif(btrim(v_fields ->> 'production_date'), '');
    if v_date_text is not null then
      begin
        v_prod_date := v_date_text::date;
      exception when others then
        v_prod_date := null;
      end;
    end if;
    if v_prod_date is null and v_inbox is not null then
      select i.received_at into v_received
        from public.biz_message_inbox i
        where i.id = v_inbox;
      if found and v_received is not null then
        v_prod_date := v_received::date;
      end if;
    end if;
    if v_prod_date is null then
      v_prod_date := pg_catalog.now()::date;
    end if;
    -- Documented default shift: daily staff reports cover the full day
    -- unless a shift was stated.
    v_shift := nullif(btrim(v_fields ->> 'shift'), '');
    if v_shift is null or v_shift not in ('morning', 'afternoon', 'night', 'full_day') then
      v_shift := 'full_day';
    end if;

    v_verified := jsonb_build_object('kind', v_kind,
      'product_id', v_product::text,
      'good_quantity', v_good,
      'rejected_quantity', v_rejected,
      'production_date', v_prod_date::text,
      'shift', v_shift);

  elsif v_kind = 'payment' then
    v_sale_ref := nullif(btrim(v_fields ->> 'sale_ref'), '');
    if v_sale_ref is null
        or v_sale_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
      raise exception 'MALFORMED: parsed payment has no sale reference to pay against';
    end if;
    -- The referenced sale must be a posted sale of this tenant/scope:
    -- resolve the reference to its submission, then to its sale.
    select s2.id into v_sale_sub
      from public.biz_review_cases c
      join public.biz_submissions s2
        on s2.tenant_id = c.tenant_id
        and s2.business_id = c.business_id
        and s2.branch_id = c.branch_id
        and s2.id = c.submission_id
      where c.tenant_id = v_tenant
        and c.review_ref = v_sale_ref;
    if not found then
      raise exception 'MALFORMED: referenced sale % is not known here', left(v_sale_ref, 13);
    end if;
    select s3.id into v_sale
      from public.biz_sales s3
      where s3.tenant_id = v_tenant and s3.business_id = v_business
        and s3.branch_id = v_branch
        and s3.source_submission_id = v_sale_sub
        and s3.status in ('confirmed', 'partially_paid', 'paid');
    if not found then
      raise exception 'MALFORMED: referenced sale % is not posted yet; confirm its sale first', left(v_sale_ref, 13);
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed payment has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed payment has no amount';
    end if;
    v_method := nullif(btrim(v_fields ->> 'method'), '');
    if v_method is null
        or v_method not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
      raise exception 'MALFORMED: parsed payment has no valid method; state cash, transfer, or pos';
    end if;

    v_verified := jsonb_build_object('kind', 'payment',
      'sale_id', v_sale::text,
      'amount_kobo', v_amount,
      'method', v_method);

  elsif v_kind = 'expense' then
    v_category := nullif(btrim(v_fields ->> 'category'), '');
    if v_category is null or v_category not in ('fuel', 'maintenance',
        'salaries', 'transport', 'packaging', 'utilities', 'rent',
        'purchases', 'other', 'task_force', 'atwap_dues',
        'tricycle_service') then
      raise exception 'MALFORMED: parsed expense has no valid category';
    end if;
    v_desc := nullif(btrim(v_fields ->> 'description'), '');
    if v_desc is null then
      v_desc := v_msg_text;
    end if;
    if v_desc is null then
      raise exception 'MALFORMED: parsed expense has no description';
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed expense has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed expense has no amount';
    end if;
    -- Documented default method: unstated staff spending is cash unless
    -- the report says otherwise.
    v_method := nullif(btrim(v_fields ->> 'payment_method'), '');
    if v_method is null or v_method not in ('cash', 'transfer', 'pos', 'other') then
      v_method := 'cash';
    end if;

    v_verified := jsonb_build_object('kind', 'expense',
      'category', v_category,
      'description', left(v_desc, 500),
      'amount_kobo', v_amount,
      'payment_method', v_method);

  elsif v_kind = 'cash_handover' then
    v_from := public._amose_resolve_intake_employee(
      v_tenant, v_business, v_branch, v_fields ->> 'from_name');
    v_to := public._amose_resolve_intake_employee(
      v_tenant, v_business, v_branch, v_fields ->> 'to_name');
    if v_from = v_to then
      raise exception 'MALFORMED: handover parties must differ; self-handover is not allowed';
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed handover has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed handover has no amount';
    end if;

    v_verified := jsonb_build_object('kind', 'cash_handover',
      'from_employee_id', v_from::text,
      'to_employee_id', v_to::text,
      'amount_kobo', v_amount);

  elsif v_kind = 'bank_deposit' then
    -- Documented fallback: a deposit report naming no depositor reads as
    -- the reporter's own deposit; the reviewer still authorizes it.
    v_text := nullif(btrim(v_fields ->> 'depositor_name'), '');
    if v_text is not null then
      v_depositor := public._amose_resolve_intake_employee(
        v_tenant, v_business, v_branch, v_text);
    else
      v_depositor := v_submitter;
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed deposit has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed deposit has no amount';
    end if;
    v_dest := nullif(btrim(v_fields ->> 'destination_account'), '');
    if v_dest is null then
      raise exception 'MALFORMED: parsed deposit has no destination account; state where it was deposited';
    end if;
    v_deposit_ref := nullif(btrim(v_fields ->> 'reference'), '');
    if v_deposit_ref is null then
      raise exception 'MALFORMED: parsed deposit has no reference; a deposit is never confirmed without a reference';
    end if;

    v_verified := jsonb_build_object('kind', 'bank_deposit',
      'deposited_by', v_depositor::text,
      'amount_kobo', v_amount,
      'destination_account', left(v_dest, 200),
      'reference', left(v_deposit_ref, 200));

  elsif v_kind = 'stock' then
    v_product := public._amose_resolve_intake_product(v_tenant, v_business);
    begin
      v_normal := (v_fields ->> 'normal_quantity')::int;
      v_cold := (v_fields ->> 'cold_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed stock count needs both normal and cold quantities';
    end;
    if v_normal is null or v_normal < 0 or v_cold is null or v_cold < 0 then
      raise exception 'MALFORMED: parsed stock count needs both normal and cold quantities';
    end if;

    v_verified := jsonb_build_object('kind', 'stock',
      'product_id', v_product::text,
      'normal_quantity', v_normal,
      'cold_quantity', v_cold);

  elsif v_kind = 'customer_payment' then
    v_customer := null;
    v_text := nullif(v_fields ->> 'customer_id', '');
    if v_text is not null then
      begin
        v_customer_id := v_text::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: parsed customer payment has invalid customer_id';
      end;
      select c.id into v_customer
        from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer_id and c.is_active = true
          and (c.branch_id is null or c.branch_id = v_branch);
      if not found then
        raise exception 'SCOPE: customer is not an active customer of this tenant/business/branch';
      end if;
    else
      v_customer := public._amose_resolve_intake_customer(
        v_tenant, v_business, v_branch, v_fields ->> 'customer_name');
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed customer payment has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed customer payment has no amount';
    end if;
    v_method := nullif(btrim(v_fields ->> 'method'), '');
    if v_method is null
        or v_method not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
      raise exception 'MALFORMED: parsed customer payment has no valid method; state cash, transfer, or pos';
    end if;

    v_verified := jsonb_build_object('kind', 'customer_payment',
      'customer_id', v_customer::text,
      'amount_kobo', v_amount,
      'method', v_method);

  elsif v_kind = 'customer_debt' then
    v_customer := null;
    v_text := nullif(v_fields ->> 'customer_id', '');
    if v_text is not null then
      begin
        v_customer_id := v_text::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: parsed customer debt has invalid customer_id';
      end;
      select c.id into v_customer
        from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer_id and c.is_active = true
          and (c.branch_id is null or c.branch_id = v_branch);
      if not found then
        raise exception 'SCOPE: customer is not an active customer of this tenant/business/branch';
      end if;
    else
      v_customer := public._amose_resolve_intake_customer(
        v_tenant, v_business, v_branch, v_fields ->> 'customer_name');
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed customer debt has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed customer debt has no amount';
    end if;

    v_verified := jsonb_build_object('kind', 'customer_debt',
      'customer_id', v_customer::text,
      'amount_kobo', v_amount);

  else
    raise exception 'UNSUPPORTED_KIND: submission kind % cannot be confirmed from chat', v_kind;
  end if;

  return public.amose_confirm_submission(
    v_ref, p_reviewer_provider, p_reviewer_sender,
    v_verified, v_key, v_reason);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 14. Least privilege restated for the replaced chat path (CREATE OR
-- REPLACE preserves existing grants; stated here so the matrix stays
-- explicit): service_role only.
-- ---------------------------------------------------------------------------
revoke execute on function public.amose_review_confirm_command(text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_review_confirm_command(text, text, text, text, text)
  to service_role;

commit;
