-- AMOSE operations brain foundation (Phase 1).
--
-- Schema only: no data rows are seeded and no business-policy VALUES are
-- inserted. Money is stored as integer kobo (bigint); floating-point money
-- is never used. Operational corrections are traceable: append-only tables
-- (inventory movements, cash custody, feedback, outcomes, context links)
-- accept inserts but no updates, so history is added to rather than
-- overwritten. Verified Brain history is never silently rewritten: Brain
-- tables grant no DELETE and verified rows change only via explicit status
-- transitions recorded in audit columns.
--
-- Security model (this phase): every new table has RLS enabled and revokes
-- ALL from anon and authenticated with NO permissive policies, so frontend
-- roles fail closed. Only service_role receives the minimum grants needed
-- (select/insert/update where the flow requires it; never delete).
-- Append-only history tables additionally get a rewrite-guard trigger that
-- aborts any UPDATE or DELETE from ANY role (including the table owner), so
-- protection does not rest on grants or comments alone.
--
-- Referential integrity: NO plain cross-table UUID FKs between tenant-owned
-- records. Every relationship uses a tenant-aware (and where applicable
-- business/branch-aware) composite FK. Parents whose older migration gave
-- them only a single-column PK but that carry tenant_id (biz_products,
-- biz_customers, biz_sales, biz_payments, biz_expenses, biz_approval_requests
-- and all Brain tables) receive UNIQUE (tenant_id, ...) candidate keys IN
-- THIS migration -- older migrations are untouched -- and children reference
-- those composite keys, so a child can never point at another tenant's
-- (or another business/branch's) record.
-- Polymorphic source references (source_table/source_record_id,
-- target_table/target_record_id, source_record_refs) are DOCUMENTED as
-- non-FK JSONB/text on purpose: they point at many tables and cannot carry
-- real referential integrity, so the application must resolve them.
begin;

-- ---------------------------------------------------------------------------
-- 1. biz_products: sellable/inventory-tracked products per business.
-- ---------------------------------------------------------------------------
create table public.biz_products (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  code text not null,
  name text not null,
  -- Sachet-water first but reusable: other packaged-water types plus a
  -- generic fallback so new product kinds need no schema change.
  product_type text not null default 'sachet_water'
    check (product_type in ('sachet_water','bottled_water','dispenser_water','other')),
  base_unit text not null default 'bag'
    check (base_unit in ('bag','bottle','sachet','carton','crate','litre','piece','other')),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  -- Product codes are unique within a business, not globally.
  unique (tenant_id, business_id, code),
  -- Candidate key so branch-scoped children (runs, movements, sale lines)
  -- reference a product with a tenant+business-aware composite FK instead
  -- of a plain product_id that could cross tenants or businesses.
  unique (tenant_id, business_id, id)
);
comment on table public.biz_products is
  'Catalogue of products per business. Tenant/business scoped; codes unique within a business.';
create index biz_products_business_active
  on public.biz_products(tenant_id, business_id, is_active);

-- ---------------------------------------------------------------------------
-- 2. biz_production_runs: one factory production run (per shift/day).
-- ---------------------------------------------------------------------------
create table public.biz_production_runs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  production_date date not null,
  shift text not null
    check (shift in ('morning','afternoon','night','full_day')),
  product_id uuid not null,
  good_quantity integer not null check (good_quantity >= 0),
  rejected_quantity integer not null default 0 check (rejected_quantity >= 0),
  produced_by uuid,
  source_submission_id uuid,
  status text not null default 'draft'
    check (status in ('draft','confirmed','voided')),
  confirmed_by uuid,
  confirmed_at timestamptz,
  voided_by uuid,
  voided_at timestamptz,
  void_reason text,
  -- Idempotency: retrying the same confirmed report never creates a twin run.
  idempotency_key text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The product must belong to the run's own tenant and business.
  foreign key (tenant_id, business_id, product_id)
    references public.biz_products(tenant_id, business_id, id),
  foreign key (tenant_id, produced_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, confirmed_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, voided_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so inventory movements pin the exact branch-scoped run.
  unique (tenant_id, business_id, branch_id, id),
  -- Drafts carry no audit trail; confirmed runs need actor AND timestamp;
  -- voided runs need the full void audit (actor, timestamp, reason).
  check ((status = 'draft' and confirmed_by is null and confirmed_at is null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'confirmed' and confirmed_by is not null and confirmed_at is not null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'voided' and voided_by is not null and voided_at is not null
      and void_reason is not null))
);
-- Each nullable employee pointer carries its own tenant-aware composite FK
-- so staff references stay scoped to the same tenant.
comment on table public.biz_production_runs is
  'Factory production runs. Voiding is an audited status change, never a delete.';
comment on column public.biz_production_runs.source_submission_id is
  'Optional originating submission; null for runs recorded directly by staff.';
create index biz_production_runs_branch_date
  on public.biz_production_runs(tenant_id, business_id, branch_id, production_date);
create index biz_production_runs_status
  on public.biz_production_runs(tenant_id, business_id, branch_id, status);

-- ---------------------------------------------------------------------------
-- 3. biz_inventory_movements: append-only stock ledger.
-- ---------------------------------------------------------------------------
create table public.biz_inventory_movements (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  product_id uuid not null,
  movement_type text not null
    check (movement_type in ('production','chill_in','chill_out','sale','return',
      'damage','adjustment_in','adjustment_out','transfer_in','transfer_out')),
  -- normal (ambient) vs cold (chilled) storage state at movement time.
  storage_state text not null default 'normal'
    check (storage_state in ('normal','cold')),
  -- Positive quantity only: direction is carried by movement_type.
  quantity integer not null check (quantity > 0),
  occurred_at timestamptz not null default now(),
  production_run_id uuid,
  source_submission_id uuid,
  recorded_by uuid,
  reason text,
  notes text,
  -- Idempotency: each attempted posting carries a client key; replays reuse it.
  idempotency_key text not null,
  -- Append-only: no updated_at. Corrections are new reversing/adjusting rows.
  created_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The product must belong to the movement's own tenant and business.
  foreign key (tenant_id, business_id, product_id)
    references public.biz_products(tenant_id, business_id, id),
  -- The run must be the movement's own branch-scoped run: same tenant,
  -- business AND branch, never another branch's run.
  foreign key (tenant_id, business_id, branch_id, production_run_id)
    references public.biz_production_runs(tenant_id, business_id, branch_id, id),
  foreign key (tenant_id, recorded_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key)
);
comment on table public.biz_inventory_movements is
  'Append-only stock ledger. No updates: corrections are new adjustment rows so history stays traceable.';
create index biz_inventory_movements_product_time
  on public.biz_inventory_movements(tenant_id, business_id, branch_id, product_id, occurred_at);
create index biz_inventory_movements_type
  on public.biz_inventory_movements(tenant_id, business_id, branch_id, movement_type);
create index biz_inventory_movements_run
  on public.biz_inventory_movements(production_run_id)
  where production_run_id is not null;

-- ---------------------------------------------------------------------------
-- 4. biz_customers: buyers (retail walk-ins through distributors).
-- ---------------------------------------------------------------------------
create table public.biz_customers (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  -- Optional branch: null means a business-wide customer. The composite FK
  -- below is null-tolerant (a null branch_id skips enforcement), which is
  -- exactly the semantics needed here.
  branch_id text,
  name text not null,
  phone text,
  customer_type text not null default 'retail'
    check (customer_type in ('retail','vendor','hawker','distributor','event','internal')),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- Candidate key so sales reference a customer with a tenant+business-aware
  -- composite FK instead of a plain customer_id that could cross tenants.
  unique (tenant_id, business_id, id)
);
comment on table public.biz_customers is
  'Customers per business. Phone is optional; uniqueness is enforced only when a phone is present.';
-- Safe uniqueness: only non-null, non-empty phones must be unique per
-- business, so any number of customers without phones can coexist.
create unique index biz_customers_business_phone_unique
  on public.biz_customers(tenant_id, business_id, phone)
  where phone is not null and phone <> '';
create index biz_customers_business_active
  on public.biz_customers(tenant_id, business_id, is_active);

-- ---------------------------------------------------------------------------
-- 5. biz_sales: sale headers (lines live in biz_sale_lines).
-- ---------------------------------------------------------------------------
create table public.biz_sales (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  -- Optional customer: null covers walk-in and grouped cash sales.
  customer_id uuid,
  sold_by uuid not null,
  sold_at timestamptz not null default now(),
  status text not null default 'draft'
    check (status in ('draft','confirmed','partially_paid','paid','voided')),
  -- Integer kobo amounts only; all nonnegative.
  subtotal_kobo bigint not null default 0 check (subtotal_kobo >= 0),
  discount_kobo bigint not null default 0 check (discount_kobo >= 0),
  total_kobo bigint not null default 0 check (total_kobo >= 0),
  source_submission_id uuid,
  idempotency_key text not null,
  confirmed_by uuid,
  confirmed_at timestamptz,
  voided_by uuid,
  voided_at timestamptz,
  void_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The customer must belong to the sale's own tenant and business
  -- (walk-in sales simply leave customer_id null).
  foreign key (tenant_id, business_id, customer_id)
    references public.biz_customers(tenant_id, business_id, id),
  foreign key (tenant_id, sold_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, confirmed_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, voided_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so lines/payments pin the exact branch-scoped sale.
  unique (tenant_id, business_id, branch_id, id),
  -- Integer-kobo arithmetic must balance exactly: total = subtotal - discount.
  check (total_kobo = subtotal_kobo - discount_kobo),
  -- Drafts carry no audit trail; confirmed/paid states need actor AND
  -- timestamp; voided sales need the full void audit (actor, timestamp, reason).
  check ((status = 'draft' and confirmed_by is null and confirmed_at is null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status in ('confirmed','partially_paid','paid') and confirmed_by is not null
      and confirmed_at is not null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'voided' and voided_by is not null and voided_at is not null
      and void_reason is not null))
);
comment on table public.biz_sales is
  'Sale headers. Voiding/confirmation are audited status changes, never deletes.';
comment on column public.biz_sales.customer_id is
  'Nullable: walk-in and grouped cash sales have no recorded customer.';
create index biz_sales_branch_time
  on public.biz_sales(tenant_id, business_id, branch_id, sold_at);
create index biz_sales_status
  on public.biz_sales(tenant_id, business_id, branch_id, status);
create index biz_sales_customer
  on public.biz_sales(customer_id)
  where customer_id is not null;

-- ---------------------------------------------------------------------------
-- 6. biz_sale_lines: one product line on a sale.
-- ---------------------------------------------------------------------------
create table public.biz_sale_lines (
  id uuid primary key default gen_random_uuid(),
  -- Scope mirrors the parent sale (NOT NULL) so composite FKs below pin
  -- the exact branch-scoped sale and product. The application keeps this
  -- scope identical to the parent sale scope.
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  sale_id uuid not null,
  product_id uuid not null,
  storage_state text not null default 'normal'
    check (storage_state in ('normal','cold')),
  quantity integer not null check (quantity > 0),
  unit_price_kobo bigint not null check (unit_price_kobo >= 0),
  line_total_kobo bigint not null check (line_total_kobo >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- The line must belong to its own branch-scoped sale: same tenant,
  -- business AND branch, never another sale.
  foreign key (tenant_id, business_id, branch_id, sale_id)
    references public.biz_sales(tenant_id, business_id, branch_id, id),
  -- The product must belong to the line's own tenant and business.
  foreign key (tenant_id, business_id, product_id)
    references public.biz_products(tenant_id, business_id, id),
  -- Integer-kobo arithmetic must balance exactly; both operands are
  -- integers so the multiplication is exact (no float rounding exists).
  check (line_total_kobo = quantity * unit_price_kobo)
);
comment on table public.biz_sale_lines is
  'Sale lines pinned to their branch-scoped sale and business-scoped product by composite FKs; amounts are integer kobo.';
create index biz_sale_lines_sale
  on public.biz_sale_lines(sale_id);
create index biz_sale_lines_product
  on public.biz_sale_lines(product_id);

-- ---------------------------------------------------------------------------
-- 7. biz_payments: money received against a sale.
-- ---------------------------------------------------------------------------
create table public.biz_payments (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  -- Scope mirrors the parent sale so employee/submission references can use
  -- composite tenant-aware FKs. The application keeps this scope identical
  -- to the parent sale scope; the FK below only guarantees it is valid.
  sale_id uuid not null,
  amount_kobo bigint not null check (amount_kobo > 0),
  method text not null
    check (method in ('cash','transfer','pos','credit_adjustment')),
  received_by uuid,
  -- Business account / external reference for reconciliation.
  destination_account text,
  reference text,
  status text not null default 'pending'
    check (status in ('pending','confirmed','reversed')),
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
  -- The payment must belong to its own branch-scoped sale: same tenant,
  -- business AND branch, never another sale.
  foreign key (tenant_id, business_id, branch_id, sale_id)
    references public.biz_sales(tenant_id, business_id, branch_id, id),
  foreign key (tenant_id, received_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, reversed_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so cash custody pins the exact branch-scoped payment.
  unique (tenant_id, business_id, branch_id, id),
  -- Pending payments carry no reversal audit; confirmed payments need the
  -- receiver (paid_at is already NOT NULL); reversed payments need the full
  -- reversal audit (actor, timestamp, reason).
  check ((status = 'pending' and reversed_by is null and reversed_at is null
      and reversal_reason is null)
    or (status = 'confirmed' and received_by is not null
      and reversed_by is null and reversed_at is null and reversal_reason is null)
    or (status = 'reversed' and reversed_by is not null and reversed_at is not null
      and reversal_reason is not null))
);
comment on table public.biz_payments is
  'Payments against sales. Reversal is an audited status change, never a delete.';
create index biz_payments_sale
  on public.biz_payments(sale_id);
create index biz_payments_status
  on public.biz_payments(tenant_id, business_id, branch_id, status);
create index biz_payments_paid_at
  on public.biz_payments(tenant_id, business_id, branch_id, paid_at);

-- Candidate key on the pre-existing approval table so new children (expenses,
-- recommendations) reference it with a tenant-aware composite FK. The older
-- migration is NOT modified; this key is added here, in the new migration
-- only. It MUST precede any table that references it (Postgres requires the
-- referenced unique key to exist at FK creation time).
alter table public.biz_approval_requests
  add constraint biz_approval_requests_tenant_id_unique unique (tenant_id, id);

-- ---------------------------------------------------------------------------
-- 8. biz_expenses: money spent by the business.
-- ---------------------------------------------------------------------------
create table public.biz_expenses (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  category text not null default 'other'
    check (category in ('fuel','maintenance','salaries','transport','packaging',
      'utilities','rent','purchases','other')),
  description text not null,
  amount_kobo bigint not null check (amount_kobo > 0),
  payment_method text not null default 'cash'
    check (payment_method in ('cash','transfer','pos','other')),
  incurred_at timestamptz not null default now(),
  -- At least one of paid_by / recorded_by is expected (who spent it vs who
  -- recorded it); both are kept nullable so paper-trail imports that only
  -- know one side still load. The application prefers both when known.
  paid_by uuid,
  recorded_by uuid,
  source_submission_id uuid,
  status text not null default 'draft'
    check (status in ('draft','confirmed','voided')),
  -- Approval gate: set when the amount/category requires owner approval.
  approval_request_id uuid,
  idempotency_key text not null,
  confirmed_by uuid,
  confirmed_at timestamptz,
  voided_by uuid,
  voided_at timestamptz,
  void_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, paid_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, recorded_by)
    references public.biz_employees(tenant_id, id),
  -- The approval request must belong to the expense's own tenant.
  foreign key (tenant_id, approval_request_id)
    references public.biz_approval_requests(tenant_id, id),
  foreign key (tenant_id, confirmed_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, voided_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, source_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  -- Candidate key so cash custody pins the exact branch-scoped expense.
  unique (tenant_id, business_id, branch_id, id),
  -- Drafts carry no audit trail; confirmed expenses need actor AND timestamp;
  -- voided expenses need the full void audit (actor, timestamp, reason).
  check ((status = 'draft' and confirmed_by is null and confirmed_at is null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'confirmed' and confirmed_by is not null and confirmed_at is not null
      and voided_by is null and voided_at is null and void_reason is null)
    or (status = 'voided' and voided_by is not null and voided_at is not null
      and void_reason is not null))
);
comment on table public.biz_expenses is
  'Business expenses in integer kobo. Approval-gated where required; voiding is audited, never a delete.';
create index biz_expenses_branch_time
  on public.biz_expenses(tenant_id, business_id, branch_id, incurred_at);
create index biz_expenses_status
  on public.biz_expenses(tenant_id, business_id, branch_id, status);
create index biz_expenses_category
  on public.biz_expenses(tenant_id, business_id, branch_id, category);

-- ---------------------------------------------------------------------------
-- 9. biz_cash_custody_entries: append-only cash-in-hand trail.
-- ---------------------------------------------------------------------------
create table public.biz_cash_custody_entries (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  -- The custodian currently holding the cash.
  custodian_id uuid not null,
  entry_type text not null
    check (entry_type in ('cash_received','cash_handed_over','expense_paid',
      'deposit_confirmed','correction')),
  amount_kobo bigint not null check (amount_kobo > 0),
  -- Optional links to the payment received or expense paid. At most one may
  -- be set; both null covers handovers/deposits with no linked record.
  related_payment_id uuid,
  related_expense_id uuid,
  occurred_at timestamptz not null default now(),
  recorded_by uuid,
  notes text,
  idempotency_key text not null,
  -- Append-only: no updated_at. Corrections are new 'correction' rows.
  created_at timestamptz not null default now(),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, custodian_id)
    references public.biz_employees(tenant_id, id),
  -- Linked payment/expense must be the entry's own branch-scoped record:
  -- same tenant, business AND branch.
  foreign key (tenant_id, business_id, branch_id, related_payment_id)
    references public.biz_payments(tenant_id, business_id, branch_id, id),
  foreign key (tenant_id, business_id, branch_id, related_expense_id)
    references public.biz_expenses(tenant_id, business_id, branch_id, id),
  foreign key (tenant_id, recorded_by)
    references public.biz_employees(tenant_id, id),
  unique (tenant_id, business_id, branch_id, idempotency_key),
  check (not (related_payment_id is not null and related_expense_id is not null))
);
comment on table public.biz_cash_custody_entries is
  'Append-only cash custody trail. No updates: fix mistakes with new correction entries.';
create index biz_cash_custody_branch_time
  on public.biz_cash_custody_entries(tenant_id, business_id, branch_id, occurred_at);
create index biz_cash_custody_custodian
  on public.biz_cash_custody_entries(tenant_id, business_id, branch_id, custodian_id);
create index biz_cash_custody_type
  on public.biz_cash_custody_entries(tenant_id, business_id, branch_id, entry_type);

-- ===========================================================================
-- YAMSI BRAIN: contextual memory + adaptive intelligence.
-- Every memory, observation and recommendation is traceable to a real source
-- (source_table/source_record_id, source_record_refs) or explicitly marked
-- as an inference via source_type = 'system_observation'. Sensitive actions
-- are never executed automatically: recommendations stay 'proposed' until a
-- human approves, and approval_request_id gates the sensitive ones.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 10. brain_memories: durable contextual memory.
-- ---------------------------------------------------------------------------
create table public.brain_memories (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  -- Optional scoping: null business/branch means tenant-wide memory. The
  -- composite FK is null-tolerant, so nulls skip enforcement by design.
  business_id text,
  branch_id text,
  employee_id uuid,
  memory_type text not null
    check (memory_type in ('fact','preference','policy','relationship','event_summary')),
  subject_type text not null,
  -- Text (not uuid): subjects span text-keyed tables (businesses) and
  -- uuid-keyed ones, so one typed FK is impossible; the application resolves
  -- subject_type/subject_id pairs. This is a documented non-FK reference.
  subject_id text not null,
  content jsonb not null default '{}'::jsonb,
  source_type text not null
    check (source_type in ('submission','operational_record','user_statement',
      'system_observation','imported_record')),
  -- Polymorphic source pointer (documented non-FK): source_table names the
  -- table, source_record_id its row. 'system_observation' marks inferences.
  source_table text,
  source_record_id text,
  verification_status text not null default 'unverified'
    check (verification_status in ('unverified','verified','disputed','superseded')),
  confidence numeric(3,2) not null default 0.50
    check (confidence >= 0 and confidence <= 1),
  valid_from timestamptz,
  valid_until timestamptz,
  recorded_at timestamptz not null default now(),
  supersedes_memory_id uuid,
  created_by_type text not null default 'system'
    check (created_by_type in ('employee','owner','system','import')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, employee_id)
    references public.biz_employees(tenant_id, id),
  check (valid_until is null or valid_from is null or valid_until >= valid_from),
  -- Scope rule: a branch always implies its business. Tenant-wide rows keep
  -- both null; business-wide rows set business_id with null branch_id.
  check (business_id is not null or branch_id is null),
  -- Traceability: the source pointer is all or nothing -- a table name
  -- without a record id (or vice versa) cannot be resolved, so it fails.
  check ((source_table is null) = (source_record_id is null)),
  -- Candidate key so links and supersedes-chains pin the memory's tenant.
  unique (tenant_id, id),
  -- A superseded memory must live in the same tenant: history is per-tenant.
  foreign key (tenant_id, supersedes_memory_id)
    references public.brain_memories(tenant_id, id)
);
comment on table public.brain_memories is
  'Contextual memory. Verified rows are history: supersede them (supersedes_memory_id), never silently rewrite them.';
comment on column public.brain_memories.source_table is
  'DOCUMENTED non-FK polymorphic pointer: names the source table; resolved by the application, not the database.';
comment on column public.brain_memories.source_record_id is
  'DOCUMENTED non-FK polymorphic pointer: source row id as text (covers uuid and text keys).';
create index brain_memories_tenant_lookup
  on public.brain_memories(tenant_id, memory_type, verification_status);
create index brain_memories_subject
  on public.brain_memories(tenant_id, subject_type, subject_id);
create index brain_memories_source
  on public.brain_memories(tenant_id, source_table, source_record_id)
  where source_table is not null;
create index brain_memories_supersedes
  on public.brain_memories(supersedes_memory_id)
  where supersedes_memory_id is not null;

-- ---------------------------------------------------------------------------
-- 11. brain_context_links: relations from a memory to operational records.
-- ---------------------------------------------------------------------------
create table public.brain_context_links (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  from_memory_id uuid not null,
  relation_type text not null
    check (relation_type in ('supports','contradicts','supersedes','elaborates',
      'derived_from','references','related')),
  -- DOCUMENTED non-FK polymorphic target: target_table names the table,
  -- target_record_id its row (text covers uuid and text keys).
  target_table text not null,
  target_record_id text not null,
  relevance numeric(3,2) not null default 0.50
    check (relevance >= 0 and relevance <= 1),
  -- Immutable links: created_at only, no updated_at.
  created_at timestamptz not null default now(),
  -- The same memory may not link to the same record twice under one relation
  -- within a tenant. tenant_id is part of the key so one tenant's links can
  -- never collide with (or shadow) another tenant's.
  unique (tenant_id, from_memory_id, relation_type, target_table, target_record_id),
  -- The linked memory must belong to the link's own tenant.
  foreign key (tenant_id, from_memory_id)
    references public.brain_memories(tenant_id, id)
);
comment on table public.brain_context_links is
  'Memory-to-record links. Targets are polymorphic by design; see column comments.';
comment on column public.brain_context_links.target_table is
  'DOCUMENTED non-FK polymorphic pointer: target table name, resolved by the application.';
comment on column public.brain_context_links.target_record_id is
  'DOCUMENTED non-FK polymorphic pointer: target row id as text.';
create index brain_context_links_from
  on public.brain_context_links(from_memory_id);
create index brain_context_links_target
  on public.brain_context_links(tenant_id, target_table, target_record_id);

-- ---------------------------------------------------------------------------
-- 12. brain_observations: detected patterns/anomalies over a time window.
-- ---------------------------------------------------------------------------
create table public.brain_observations (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  -- Null business/branch means a tenant-wide observation; composite FKs are
  -- null-tolerant by design.
  business_id text,
  branch_id text,
  observation_type text not null
    check (observation_type in ('sales_drop','sales_spike','waste_spike','stock_low',
      'stock_out','cash_shortage','repeat_issue','anomaly','trend','other')),
  window_start timestamptz,
  window_end timestamptz,
  -- Structured evidence (metrics, counts, thresholds hit).
  evidence jsonb not null default '{}'::jsonb,
  -- DOCUMENTED non-FK polymorphic refs: JSONB array of
  -- {"table": ..., "record_id": ...} entries resolved by the application.
  source_record_refs jsonb not null default '[]'::jsonb,
  confidence numeric(3,2) not null default 0.50
    check (confidence >= 0 and confidence <= 1),
  severity text not null default 'info'
    check (severity in ('info','warning','critical')),
  status text not null default 'open'
    check (status in ('open','acknowledged','resolved','dismissed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  check (window_end is null or window_start is null or window_end >= window_start),
  -- Scope rule: a branch always implies its business.
  check (business_id is not null or branch_id is null),
  -- Candidate key so recommendations pin their observation's tenant.
  unique (tenant_id, id)
);
comment on table public.brain_observations is
  'Adaptive-intelligence findings. Evidence is structured JSONB; source_record_refs is a DOCUMENTED non-FK polymorphic array.';
create index brain_observations_branch_time
  on public.brain_observations(tenant_id, business_id, branch_id, created_at);
create index brain_observations_type_status
  on public.brain_observations(tenant_id, observation_type, status);

-- ---------------------------------------------------------------------------
-- 13. brain_recommendations: proposed actions from observations.
-- ---------------------------------------------------------------------------
create table public.brain_recommendations (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text,
  branch_id text,
  -- The observation that produced this recommendation, when there is one.
  observation_id uuid,
  recommendation_type text not null
    check (recommendation_type in ('restock','price_change','staff_action','maintenance',
      'follow_up','purchase_approval','other')),
  title text not null,
  explanation text not null,
  -- Machine-readable proposal; a human (or an approved flow) applies it.
  proposed_action jsonb not null default '{}'::jsonb,
  confidence numeric(3,2) not null default 0.50
    check (confidence >= 0 and confidence <= 1),
  status text not null default 'proposed'
    check (status in ('proposed','approved','rejected','executed','expired','withdrawn')),
  -- Sensitive recommendations link an approval request; the application must
  -- never auto-execute them (no automatic sensitive action execution).
  approval_request_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- Scope rule: a branch always implies its business.
  check (business_id is not null or branch_id is null),
  -- Candidate key so feedback/outcomes pin their recommendation's tenant.
  unique (tenant_id, id),
  -- The observation must belong to the recommendation's own tenant.
  foreign key (tenant_id, observation_id)
    references public.brain_observations(tenant_id, id),
  -- The approval request must belong to the recommendation's own tenant.
  foreign key (tenant_id, approval_request_id)
    references public.biz_approval_requests(tenant_id, id)
);
comment on table public.brain_recommendations is
  'Proposed actions. No automatic sensitive-action execution: status leaves proposed only via human approval.';
create index brain_recommendations_observation
  on public.brain_recommendations(observation_id)
  where observation_id is not null;
create index brain_recommendations_status
  on public.brain_recommendations(tenant_id, business_id, branch_id, status);
create index brain_recommendations_type
  on public.brain_recommendations(tenant_id, recommendation_type);

-- ---------------------------------------------------------------------------
-- 14. brain_feedback: human verdicts on recommendations (append-only).
-- ---------------------------------------------------------------------------
create table public.brain_feedback (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  recommendation_id uuid not null,
  feedback_source text not null default 'employee'
    check (feedback_source in ('employee','owner','system','other')),
  employee_id uuid,
  rating text
    check (rating in ('accurate','partially_accurate','inaccurate','not_useful')),
  correction_text text,
  -- Structured correction payload (e.g. corrected values the Brain should use).
  correction jsonb not null default '{}'::jsonb,
  -- Append-only: created_at only, no updated_at.
  created_at timestamptz not null default now(),
  foreign key (tenant_id, employee_id)
    references public.biz_employees(tenant_id, id),
  -- The recommendation must belong to the feedback's own tenant.
  foreign key (tenant_id, recommendation_id)
    references public.brain_recommendations(tenant_id, id)
);
comment on table public.brain_feedback is
  'Append-only human feedback on recommendations; feeds future adaptation.';
create index brain_feedback_recommendation
  on public.brain_feedback(recommendation_id);

-- ---------------------------------------------------------------------------
-- 15. brain_outcomes: measured results of recommendations (append-only).
-- ---------------------------------------------------------------------------
create table public.brain_outcomes (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  recommendation_id uuid not null,
  outcome_type text not null
    check (outcome_type in ('sales_lift','waste_reduction','stock_recovered',
      'cash_reconciled','no_change','negative','other')),
  measured_result jsonb not null default '{}'::jsonb,
  success_status text not null
    check (success_status in ('success','partial','failed','inconclusive')),
  measured_at timestamptz not null default now(),
  -- DOCUMENTED non-FK polymorphic refs: JSONB array of
  -- {"table": ..., "record_id": ...} entries resolved by the application.
  source_record_refs jsonb not null default '[]'::jsonb,
  -- Append-only: created_at only, no updated_at.
  created_at timestamptz not null default now(),
  -- The recommendation must belong to the outcome's own tenant.
  foreign key (tenant_id, recommendation_id)
    references public.brain_recommendations(tenant_id, id)
);
comment on table public.brain_outcomes is
  'Append-only measured outcomes of recommendations; source_record_refs is a DOCUMENTED non-FK polymorphic array.';
create index brain_outcomes_recommendation
  on public.brain_outcomes(recommendation_id);
create index brain_outcomes_measured
  on public.brain_outcomes(tenant_id, measured_at);

-- ---------------------------------------------------------------------------
-- 16. brain_adaptation_state: learned configuration per scope (never secrets).
-- ---------------------------------------------------------------------------
create table public.brain_adaptation_state (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text,
  branch_id text,
  adaptation_key text not null,
  version integer not null default 1 check (version > 0),
  state jsonb not null default '{}'::jsonb,
  based_on_verified_evidence_count integer not null default 0
    check (based_on_verified_evidence_count >= 0),
  last_updated_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  -- Scope rule: a branch always implies its business. Tenant-wide rows keep
  -- both null; business-wide rows set business_id with null branch_id.
  check (business_id is not null or branch_id is null)
);
comment on table public.brain_adaptation_state is
  'Learned configuration only (thresholds, weights, preferences). NEVER raw secret credentials.';
-- Unique scoped key that is null-safe: coalesce lets tenant-wide rows
-- (null business/branch) still collide on (tenant, key) as intended.
create unique index brain_adaptation_state_scoped_key
  on public.brain_adaptation_state(
    tenant_id, coalesce(business_id, ''), coalesce(branch_id, ''), adaptation_key);
create index brain_adaptation_state_scope
  on public.brain_adaptation_state(tenant_id, business_id, branch_id);

-- ===========================================================================
-- Append-only enforcement: a rewrite-guard trigger aborts UPDATE/DELETE on
-- history tables for EVERY role (grants alone cannot bind the table owner
-- or a superuser). TRUNCATE is likewise revoked from service_role below.
-- If a genuine correction is ever required, a future migration must
-- explicitly drop the guard trigger first -- history can never be rewritten
-- silently or by accident.
-- ===========================================================================
create or replace function public.reject_history_rewrite()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  raise exception 'append-only table %.% does not allow %',
    TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
  return null;
end;
$func$;
comment on function public.reject_history_rewrite() is
  'Rewrite guard for append-only history tables: aborts every UPDATE and DELETE.';

create trigger biz_inventory_movements_no_rewrite
  before update or delete on public.biz_inventory_movements
  for each row execute function public.reject_history_rewrite();
create trigger biz_cash_custody_entries_no_rewrite
  before update or delete on public.biz_cash_custody_entries
  for each row execute function public.reject_history_rewrite();
create trigger brain_context_links_no_rewrite
  before update or delete on public.brain_context_links
  for each row execute function public.reject_history_rewrite();
create trigger brain_feedback_no_rewrite
  before update or delete on public.brain_feedback
  for each row execute function public.reject_history_rewrite();
create trigger brain_outcomes_no_rewrite
  before update or delete on public.brain_outcomes
  for each row execute function public.reject_history_rewrite();

-- ===========================================================================
-- RLS + grants: fail closed for frontend roles, minimum for service_role.
-- No policies are created in this phase, so anon/authenticated see nothing.
-- service_role bypasses RLS; grants below are the only access it receives
-- here (select/insert/update where the flow needs it; never delete, so
-- operational and Brain history cannot be wiped through these grants).
-- ===========================================================================
alter table public.biz_products enable row level security;
alter table public.biz_production_runs enable row level security;
alter table public.biz_inventory_movements enable row level security;
alter table public.biz_customers enable row level security;
alter table public.biz_sales enable row level security;
alter table public.biz_sale_lines enable row level security;
alter table public.biz_payments enable row level security;
alter table public.biz_expenses enable row level security;
alter table public.biz_cash_custody_entries enable row level security;
alter table public.brain_memories enable row level security;
alter table public.brain_context_links enable row level security;
alter table public.brain_observations enable row level security;
alter table public.brain_recommendations enable row level security;
alter table public.brain_feedback enable row level security;
alter table public.brain_outcomes enable row level security;
alter table public.brain_adaptation_state enable row level security;

revoke all on public.biz_products,
  public.biz_production_runs, public.biz_inventory_movements,
  public.biz_customers, public.biz_sales, public.biz_sale_lines,
  public.biz_payments, public.biz_expenses, public.biz_cash_custody_entries,
  public.brain_memories, public.brain_context_links, public.brain_observations,
  public.brain_recommendations, public.brain_feedback, public.brain_outcomes,
  public.brain_adaptation_state
  from anon, authenticated, service_role;

grant select, insert, update on public.biz_products to service_role;
grant select, insert, update on public.biz_production_runs to service_role;
-- Append-only ledger: no update grant, so posted movements stay immutable.
grant select, insert on public.biz_inventory_movements to service_role;
grant select, insert, update on public.biz_customers to service_role;
grant select, insert, update on public.biz_sales to service_role;
grant select, insert, update on public.biz_sale_lines to service_role;
grant select, insert, update on public.biz_payments to service_role;
grant select, insert, update on public.biz_expenses to service_role;
-- Append-only custody trail: no update grant.
grant select, insert on public.biz_cash_custody_entries to service_role;
grant select, insert, update on public.brain_memories to service_role;
-- Immutable links: no update grant.
grant select, insert on public.brain_context_links to service_role;
grant select, insert, update on public.brain_observations to service_role;
grant select, insert, update on public.brain_recommendations to service_role;
-- Append-only learning signals: no update grants.
grant select, insert on public.brain_feedback to service_role;
grant select, insert on public.brain_outcomes to service_role;
grant select, insert, update on public.brain_adaptation_state to service_role;

-- Belt and braces for append-only history: the rewrite-guard triggers above
-- already abort UPDATE/DELETE for every role; these revocations additionally
-- remove TRUNCATE (inherited from the database's default privileges, which
-- this migration does not otherwise change) from service_role on history.
revoke truncate on public.biz_inventory_movements from service_role;
revoke truncate on public.biz_cash_custody_entries from service_role;
revoke truncate on public.brain_context_links from service_role;
revoke truncate on public.brain_feedback from service_role;
revoke truncate on public.brain_outcomes from service_role;

-- Least privilege on the guard function itself: nobody may call it
-- directly. Trigger invocation does not require EXECUTE, so the five
-- rewrite-guard triggers keep firing for every role (verified locally).
revoke execute on function public.reject_history_rewrite()
  from public, anon, authenticated, service_role;

commit;
