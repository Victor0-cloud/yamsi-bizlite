-- Phase 2: controlled operational posting layer for AMOSE and the YAMSI Brain.
--
-- Schema + code only: no data rows are seeded and no business-policy VALUES
-- are inserted. Money stays integer kobo (bigint); floats are never used.
--
-- Submission-kind mapping (explicit; the repository's real submission kinds
-- differ from the four posting types, so the mapping is documented here and
-- mirrored in operational_posting.py -- the two must stay in sync):
--
--   submission kind          posting type   RPC
--   -----------------------  ------------   ---------------------------------
--   'production'             production     public.amose_post_production(uuid)
--   'poultry_daily_report'   production     public.amose_post_production(uuid)
--   'sale'                   sale           public.amose_post_sale(uuid)
--   'payment'                payment        public.amose_post_payment(uuid)
--   'expense'                expense        public.amose_post_expense(uuid)
--   anything else            --             rejected, no records created
--
-- 'poultry_daily_report' (the nughe_farms/warri report shape produced by
-- rule_engine.extract) routes to the production RPC, but legacy poultry
-- fields alone are NOT enough: every RPC reads ONLY the confirmation-time
-- 'verified' object inside biz_submissions.payload (populated by a human
-- confirmation step, never by WhatsApp ingestion). A confirmed submission
-- without a well-formed 'verified' block is rejected as malformed.
--
-- Verified block contract (all ids are uuid text, all money integer kobo):
--   production: {product_id, good_quantity>=0, rejected_quantity>=0=0,
--     production_date 'YYYY-MM-DD', shift, produced_by?=submission employee}
--   sale: {lines: [{product_id, quantity>0, unit_price_kobo>=0,
--     storage_state?='normal'}] (>=1), customer_id?, sold_at?, discount_kobo?=0,
--     payment?: {amount_kobo>0, method, received_by?, destination_account?,
--     reference?, paid_at?}}
--   payment: {sale_id, amount_kobo>0, method, received_by?,
--     destination_account?, reference?, paid_at?}
--   expense: {category, description non-empty, amount_kobo>0, payment_method,
--     incurred_at?, paid_by?, recorded_by?, approval_request_id?}
--
-- Design guarantees (every RPC):
-- - One function call = one transaction. Any failure rolls back everything.
-- - Only a status='confirmed' submission posts; draft/pending/rejected/voided
--   (or missing) submissions fail closed with no writes.
-- - The submission row is locked (FOR UPDATE) so concurrent duplicate posts
--   serialize instead of doubling records.
-- - Tenant/business/branch scope is taken ONLY from the locked submission row
--   -- never from caller-supplied identifiers (the RPC takes a single uuid
--   argument). Every referenced product/employee/customer/sale/approval is
--   re-validated against that authoritative scope inside the transaction.
-- - Deterministic idempotency keys 'posting:<submission_id>:<role>' make
--   retries return the already-created result without duplicating runs,
--   movements, sales, lines, payments, expenses, custody entries or memories.
--   Before reporting already_posted, each RPC re-verifies the prior posting
--   is complete (exact line/movement/link counts, payment/custody presence
--   where applicable); an incomplete prior posting fails closed as
--   INCOMPLETE instead of reporting success for partial data.
-- - Payment integrity: embedded payments must not exceed the verified sale
--   total; standalone payments lock the sale, sum confirmed payments only
--   (pending/reversed never count), and must fit the unpaid balance.
--   Confirmed payments atomically move the sale (confirmed -> partially_paid
--   -> paid). Overpayment and payments against voided sales roll everything
--   back. Sale status/balance are always calculated, never caller-supplied.
-- - This layer is intentionally UNREACHABLE end-to-end until a controlled
--   human confirmation step exists that writes payload.verified. No such
--   writer is created or wired here; without a verified block every RPC
--   refuses as malformed.
-- - Brain memory is written in the SAME transaction (verified facts only, no
--   narrative/metrics/invented confidence, source-traced to the submission).
--   No recommendations, feedback, outcomes or adaptation-state rows are
--   touched here -- adaptive intelligence stays a later phase.
-- - No dynamic SQL: table/column names are fixed; the only input is the
--   submission uuid. SECURITY DEFINER with a fixed search_path; EXECUTE is
--   revoked from PUBLIC/anon/authenticated and granted only to service_role.
begin;

-- ---------------------------------------------------------------------------
-- Shared guard: lock + fetch the submission, enforce confirmed status and an
-- allowlisted kind. Returns the row fields via OUT params so each RPC keeps
-- one linear body without dynamic SQL.
-- ---------------------------------------------------------------------------
create or replace function public._amose_lock_confirmed_submission(
  p_submission_id uuid,
  p_allowed_kinds text[],
  out o_tenant_id uuid,
  out o_business_id text,
  out o_branch_id text,
  out o_submission_id uuid,
  out o_employee_id uuid,
  out o_kind text,
  out o_payload jsonb
)
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_status text;
begin
  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.payload
    into o_tenant_id, o_business_id, o_branch_id, o_submission_id,
      o_employee_id, o_kind, v_status, o_payload
    from public.biz_submissions s
    where s.id = p_submission_id
    for update;
  if not found then
    raise exception 'NOT_FOUND: submission % does not exist', p_submission_id;
  end if;
  if v_status is distinct from 'confirmed' then
    raise exception 'UNCONFIRMED: submission % has status %, only confirmed submissions post',
      p_submission_id, v_status;
  end if;
  if o_kind is null or not (o_kind = any (p_allowed_kinds)) then
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, expected one of %',
      p_submission_id, o_kind, array_to_string(p_allowed_kinds, ',');
  end if;
end;
$func$;

-- ---------------------------------------------------------------------------
-- 1. production: one confirmed biz_production_runs row + the matching
-- append-only production inventory movement + one verified Brain memory.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_production(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_product uuid; v_good int; v_rejected int := 0;
  v_prod_date date; v_shift text; v_produced_by uuid;
  v_key_run text; v_key_move text;
  v_run_id uuid; v_move_id uuid := null; v_mem_id uuid;
  v_existing jsonb;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['production', 'poultry_daily_report']);

  -- Idempotent retry: the run's deterministic key already exists.
  v_key_run := 'posting:' || v_sub::text || ':production_run';
  select r.id, r.good_quantity into v_run_id, v_good
    from public.biz_production_runs r
    where r.tenant_id = v_tenant and r.business_id = v_business
      and r.branch_id = v_branch and r.idempotency_key = v_key_run;
  if found then
    v_key_move := 'posting:' || v_sub::text || ':production_movement';
    select m.id into v_move_id
      from public.biz_inventory_movements m
      where m.tenant_id = v_tenant and m.business_id = v_business
        and m.branch_id = v_branch and m.idempotency_key = v_key_move;
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'production_run';
    -- Retry completeness: a prior posting must be whole. A zero-good run
    -- posts no movement; anything else missing fails closed instead of
    -- reporting success for partial data.
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % production posting is missing its brain memory', v_sub;
    end if;
    if v_good > 0 and v_move_id is null then
      raise exception 'INCOMPLETE: submission % production posting is missing its inventory movement', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_production_runs'
          and l.target_record_id = v_run_id::text) then
      raise exception 'INCOMPLETE: submission % production posting is missing its run context link', v_sub;
    end if;
    if v_move_id is not null
        and not exists (select 1 from public.brain_context_links l
          where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
            and l.relation_type = 'derived_from'
            and l.target_table = 'biz_inventory_movements'
            and l.target_record_id = v_move_id::text) then
      raise exception 'INCOMPLETE: submission % production posting is missing its movement context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'production',
      'submission_id', v_sub::text, 'production_run_id', v_run_id::text,
      'inventory_movement_id', v_move_id::text, 'brain_memory_id', v_mem_id::text,
      'is_retry', true);
  end if;

  -- Verified block (confirmation-time facts only; never raw WhatsApp text).
  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified production block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read (top-level payload
  -- values cannot override anything). A kind label inside verified, when
  -- present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  begin
    v_product := nullif(v_ver ->> 'product_id', '')::uuid;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % has invalid product_id', v_sub;
  end;
  if v_product is null then
    raise exception 'MALFORMED: submission % verified block requires product_id', v_sub;
  end if;
  begin
    v_good := coalesce((v_ver ->> 'good_quantity')::int, -1);
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires good_quantity >= 0', v_sub;
  end;
  if v_good is null or v_good < 0 then
    raise exception 'MALFORMED: submission % verified block requires good_quantity >= 0', v_sub;
  end if;
  if v_ver ? 'rejected_quantity' then
    begin
      v_rejected := (v_ver ->> 'rejected_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block requires rejected_quantity >= 0', v_sub;
    end;
    if v_rejected is null or v_rejected < 0 then
      raise exception 'MALFORMED: submission % verified block requires rejected_quantity >= 0', v_sub;
    end if;
  end if;
  begin
    v_prod_date := (v_ver ->> 'production_date')::date;
  exception when others then
    raise exception 'MALFORMED: submission % verified block requires production_date YYYY-MM-DD', v_sub;
  end;
  if v_prod_date is null then
    raise exception 'MALFORMED: submission % verified block requires production_date', v_sub;
  end if;
  v_shift := v_ver ->> 'shift';
  if v_shift is null or v_shift not in ('morning', 'afternoon', 'night', 'full_day') then
    raise exception 'MALFORMED: submission % verified block requires a valid shift', v_sub;
  end if;
  v_produced_by := v_emp;
  if v_ver ? 'produced_by' and nullif(v_ver ->> 'produced_by', '') is not null then
    begin
      v_produced_by := (v_ver ->> 'produced_by')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % has invalid produced_by', v_sub;
    end;
  end if;

  -- Scope checks against the authoritative submission scope.
  if not exists (select 1 from public.biz_products p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.id = v_product and p.is_active) then
    raise exception 'SCOPE: product % is not an active product of this tenant/business', v_product;
  end if;
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = v_tenant and e.id = v_produced_by) then
    raise exception 'SCOPE: employee % is not in this tenant', v_produced_by;
  end if;

  insert into public.biz_production_runs
    (tenant_id, business_id, branch_id, production_date, shift, product_id,
     good_quantity, rejected_quantity, produced_by, source_submission_id,
     status, confirmed_by, confirmed_at, idempotency_key)
  values (v_tenant, v_business, v_branch, v_prod_date, v_shift, v_product,
    v_good, v_rejected, v_produced_by, v_sub,
    'confirmed', v_emp, now(), v_key_run)
  returning id into v_run_id;

  -- Matching append-only movement. A zero-good run posts no movement row
  -- (the ledger requires quantity > 0); the run itself is still recorded.
  if v_good > 0 then
    v_key_move := 'posting:' || v_sub::text || ':production_movement';
    insert into public.biz_inventory_movements
      (tenant_id, business_id, branch_id, product_id, movement_type,
       storage_state, quantity, production_run_id, source_submission_id,
       recorded_by, reason, idempotency_key)
    values (v_tenant, v_business, v_branch, v_product, 'production',
      'normal', v_good, v_run_id, v_sub,
      v_emp, 'confirmed production posting', v_key_move)
    returning id into v_move_id;
  end if;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'production_run';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'production_run', v_run_id::text,
      jsonb_build_object('production_run_id', v_run_id::text,
        'product_id', v_product::text, 'good_quantity', v_good,
        'rejected_quantity', v_rejected, 'production_date', v_prod_date::text,
        'shift', v_shift),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_production_runs', v_run_id::text)
  on conflict do nothing;
  if v_move_id is not null then
    insert into public.brain_context_links
      (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
    values (v_tenant, v_mem_id, 'derived_from', 'biz_inventory_movements', v_move_id::text)
    on conflict do nothing;
  end if;

  return jsonb_build_object('status', 'posted', 'posting_type', 'production',
    'submission_id', v_sub::text, 'production_run_id', v_run_id::text,
    'inventory_movement_id', v_move_id::text, 'brain_memory_id', v_mem_id::text,
    'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 2. sale: one confirmed biz_sales header + its biz_sale_lines + one
-- append-only 'sale' inventory movement per line + (only when the verified
-- block carries payment details) one confirmed biz_payments row and its
-- matching cash-custody entry + one verified Brain memory. All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_sale(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb; v_lines jsonb; v_pay jsonb;
  v_customer uuid := null; v_sold_at timestamptz; v_discount bigint := 0;
  v_subtotal bigint := 0; v_total bigint;
  v_key_sale text;
  v_sale_id uuid; v_pay_id uuid := null; v_custody_id uuid := null; v_mem_id uuid;
  v_line record; v_idx int := 0;
  v_l_product uuid; v_l_qty int; v_l_price bigint; v_l_total bigint;
  v_l_storage text; v_line_id uuid; v_line_ids uuid[] := '{}';
  v_p_amount bigint; v_p_method text; v_p_received uuid;
  v_p_dest text; v_p_ref text; v_p_at timestamptz;
  v_key_pay text; v_key_cust text;
  v_exp_lines int; v_got_lines int; v_got_moves int; v_got_llinks int;
  v_pay_expected boolean;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['sale']);

  -- Idempotent retry: the sale header's deterministic key already exists.
  -- Lines/movements belong to that sale, so the stored sale is re-read and
  -- returned without inserting anything.
  v_key_sale := 'posting:' || v_sub::text || ':sale';
  select s.id into v_sale_id
    from public.biz_sales s
    where s.tenant_id = v_tenant and s.business_id = v_business
      and s.branch_id = v_branch and s.idempotency_key = v_key_sale;
  if found then
    v_key_pay := 'posting:' || v_sub::text || ':sale_payment';
    select p.id into v_pay_id
      from public.biz_payments p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.branch_id = v_branch and p.idempotency_key = v_key_pay;
    v_key_cust := 'posting:' || v_sub::text || ':sale_custody';
    select c.id into v_custody_id
      from public.biz_cash_custody_entries c
      where c.tenant_id = v_tenant and c.business_id = v_business
        and c.branch_id = v_branch and c.idempotency_key = v_key_cust;
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'sale';
    -- Retry completeness: the prior posting must be whole -- exact line and
    -- movement counts re-derived from the verified block, the memory plus
    -- every context link, and payment/custody exactly when the verified
    -- block included payment details. Anything less fails closed.
    if v_payload -> 'verified' is null
        or jsonb_typeof(v_payload -> 'verified') <> 'object'
        or jsonb_typeof(v_payload -> 'verified' -> 'lines') <> 'array' then
      raise exception 'INCOMPLETE: submission % sale posting cannot be verified (verified block unreadable)', v_sub;
    end if;
    v_exp_lines := jsonb_array_length(v_payload -> 'verified' -> 'lines');
    select count(*) into v_got_lines from public.biz_sale_lines l
      where l.tenant_id = v_tenant and l.business_id = v_business
        and l.branch_id = v_branch and l.sale_id = v_sale_id;
    select count(*) into v_got_moves from public.biz_inventory_movements m
      where m.tenant_id = v_tenant and m.business_id = v_business
        and m.branch_id = v_branch and m.movement_type = 'sale'
        and m.source_submission_id = v_sub;
    v_pay_expected := jsonb_typeof(v_payload -> 'verified' -> 'payment') = 'object';
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % sale posting is missing its brain memory', v_sub;
    end if;
    if v_got_lines <> v_exp_lines or v_got_lines < 1 then
      raise exception 'INCOMPLETE: submission % sale posting has % of % expected lines',
        v_sub, v_got_lines, v_exp_lines;
    end if;
    if v_got_moves <> v_exp_lines then
      raise exception 'INCOMPLETE: submission % sale posting has % of % expected movements',
        v_sub, v_got_moves, v_exp_lines;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_sales'
          and l.target_record_id = v_sale_id::text) then
      raise exception 'INCOMPLETE: submission % sale posting is missing its sale context link', v_sub;
    end if;
    select count(*) into v_got_llinks from public.brain_context_links l
      where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
        and l.relation_type = 'derived_from'
        and l.target_table = 'biz_sale_lines';
    if v_got_llinks <> v_exp_lines then
      raise exception 'INCOMPLETE: submission % sale posting has % of % expected line context links',
        v_sub, v_got_llinks, v_exp_lines;
    end if;
    if v_pay_expected and v_pay_id is null then
      raise exception 'INCOMPLETE: submission % sale posting is missing its payment', v_sub;
    end if;
    if v_pay_id is not null and v_custody_id is null then
      raise exception 'INCOMPLETE: submission % sale posting is missing its cash custody entry', v_sub;
    end if;
    if v_pay_id is not null
        and not exists (select 1 from public.brain_context_links l
          where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
            and l.relation_type = 'derived_from'
            and l.target_table = 'biz_payments'
            and l.target_record_id = v_pay_id::text) then
      raise exception 'INCOMPLETE: submission % sale posting is missing its payment context link', v_sub;
    end if;
    if v_custody_id is not null
        and not exists (select 1 from public.brain_context_links l
          where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
            and l.relation_type = 'derived_from'
            and l.target_table = 'biz_cash_custody_entries'
            and l.target_record_id = v_custody_id::text) then
      raise exception 'INCOMPLETE: submission % sale posting is missing its custody context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'sale',
      'submission_id', v_sub::text, 'sale_id', v_sale_id::text,
      'payment_id', v_pay_id::text, 'cash_custody_entry_id', v_custody_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified sale block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read (top-level payload
  -- values cannot override anything). A kind label inside verified, when
  -- present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  v_lines := v_ver -> 'lines';
  if v_lines is null or jsonb_typeof(v_lines) <> 'array'
      or jsonb_array_length(v_lines) < 1 then
    raise exception 'MALFORMED: submission % verified block requires at least one sale line', v_sub;
  end if;

  -- Optional customer: null covers walk-in/grouped cash sales. A recorded
  -- customer must belong to this tenant/business (business-wide when its own
  -- branch is null, otherwise this exact branch).
  if v_ver ? 'customer_id' and nullif(v_ver ->> 'customer_id', '') is not null then
    begin
      v_customer := (v_ver ->> 'customer_id')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % has invalid customer_id', v_sub;
    end;
    if not exists (select 1 from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer
          and (c.branch_id is null or c.branch_id = v_branch)) then
      raise exception 'SCOPE: customer % is not a customer of this tenant/business/branch', v_customer;
    end if;
  end if;

  -- Optional discount (integer kobo, never above the subtotal).
  if v_ver ? 'discount_kobo' then
    begin
      v_discount := (v_ver ->> 'discount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid discount_kobo', v_sub;
    end;
    if v_discount is null or v_discount < 0 then
      raise exception 'MALFORMED: submission % verified block requires discount_kobo >= 0', v_sub;
    end if;
  end if;

  -- Optional sold_at; defaults to now.
  v_sold_at := now();
  if v_ver ? 'sold_at' and nullif(v_ver ->> 'sold_at', '') is not null then
    begin
      v_sold_at := (v_ver ->> 'sold_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid sold_at', v_sub;
    end;
  end if;

  -- First pass over lines: validate scope + arithmetic, accumulate subtotal.
  for v_line in select value as item from jsonb_array_elements(v_lines) as value loop
    v_idx := v_idx + 1;
    if jsonb_typeof(v_line.item) <> 'object' then
      raise exception 'MALFORMED: submission % sale line % is not an object', v_sub, v_idx;
    end if;
    begin
      v_l_product := nullif(v_line.item ->> 'product_id', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % sale line % has invalid product_id', v_sub, v_idx;
    end;
    if v_l_product is null then
      raise exception 'MALFORMED: submission % sale line % requires product_id', v_sub, v_idx;
    end if;
    if not exists (select 1 from public.biz_products p
        where p.tenant_id = v_tenant and p.business_id = v_business
          and p.id = v_l_product and p.is_active) then
      raise exception 'SCOPE: product % on sale line % is not an active product of this tenant/business',
        v_l_product, v_idx;
    end if;
    begin
      v_l_qty := (v_line.item ->> 'quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % sale line % requires quantity > 0', v_sub, v_idx;
    end;
    if v_l_qty is null or v_l_qty <= 0 then
      raise exception 'MALFORMED: submission % sale line % requires quantity > 0', v_sub, v_idx;
    end if;
    begin
      v_l_price := (v_line.item ->> 'unit_price_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % sale line % requires unit_price_kobo >= 0', v_sub, v_idx;
    end;
    if v_l_price is null or v_l_price < 0 then
      raise exception 'MALFORMED: submission % sale line % requires unit_price_kobo >= 0', v_sub, v_idx;
    end if;
    v_l_storage := coalesce(nullif(v_line.item ->> 'storage_state', ''), 'normal');
    if v_l_storage not in ('normal', 'cold') then
      raise exception 'MALFORMED: submission % sale line % has invalid storage_state', v_sub, v_idx;
    end if;
    v_subtotal := v_subtotal + (v_l_qty::bigint * v_l_price);
  end loop;
  if v_discount > v_subtotal then
    raise exception 'MALFORMED: submission % discount % exceeds subtotal %', v_sub, v_discount, v_subtotal;
  end if;
  v_total := v_subtotal - v_discount;

  -- Optional embedded payment: created only when verified payment details
  -- are present; validated now so a bad payment block fails the whole sale.
  v_pay := v_ver -> 'payment';
  if v_pay is not null then
    if jsonb_typeof(v_pay) <> 'object' then
      raise exception 'MALFORMED: submission % verified payment block is not an object', v_sub;
    end if;
    begin
      v_p_amount := (v_pay ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified payment block requires amount_kobo > 0', v_sub;
    end;
    if v_p_amount is null or v_p_amount <= 0 then
      raise exception 'MALFORMED: submission % verified payment block requires amount_kobo > 0', v_sub;
    end if;
    v_p_method := v_pay ->> 'method';
    if v_p_method is null or v_p_method not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
      raise exception 'MALFORMED: submission % verified payment block requires a valid method', v_sub;
    end if;
    v_p_received := v_emp;
    if v_pay ? 'received_by' and nullif(v_pay ->> 'received_by', '') is not null then
      begin
        v_p_received := (v_pay ->> 'received_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: submission % verified payment block has invalid received_by', v_sub;
      end;
    end if;
    if not exists (select 1 from public.biz_employees e
        where e.tenant_id = v_tenant and e.id = v_p_received) then
      raise exception 'SCOPE: employee % is not in this tenant', v_p_received;
    end if;
    v_p_dest := nullif(v_pay ->> 'destination_account', '');
    v_p_ref := nullif(v_pay ->> 'reference', '');
    v_p_at := now();
    if v_pay ? 'paid_at' and nullif(v_pay ->> 'paid_at', '') is not null then
      begin
        v_p_at := (v_pay ->> 'paid_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: submission % verified payment block has invalid paid_at', v_sub;
      end;
    end if;
    -- Embedded payment integrity: this sale has no prior payments, so the
    -- embedded amount must be positive (checked above) and must not exceed
    -- the verified sale total. Overpayment fails the whole posting.
    if v_p_amount > v_total then
      raise exception 'MALFORMED: submission % embedded payment % exceeds sale total %',
        v_sub, v_p_amount, v_total;
    end if;
  end if;

  insert into public.biz_sales
    (tenant_id, business_id, branch_id, customer_id, sold_by, sold_at,
     status, subtotal_kobo, discount_kobo, total_kobo,
     source_submission_id, idempotency_key, confirmed_by, confirmed_at)
  values (v_tenant, v_business, v_branch, v_customer, v_emp, v_sold_at,
    -- Status is derived from calculated values only: no payment yet means
    -- confirmed; an embedded payment below total means partially_paid; an
    -- embedded payment equal to total means paid. Caller-supplied status is
    -- never accepted (the RPC takes no status argument at all).
    (case when v_pay is null then 'confirmed'
      when v_p_amount >= v_total then 'paid'
      else 'partially_paid' end),
    v_subtotal, v_discount, v_total,
    v_sub, v_key_sale, v_emp, now())
  returning id into v_sale_id;

  -- Second pass: lines + one append-only 'sale' movement per line.
  v_idx := 0;
  for v_line in select value as item from jsonb_array_elements(v_lines) as value loop
    v_idx := v_idx + 1;
    v_l_product := (v_line.item ->> 'product_id')::uuid;
    v_l_qty := (v_line.item ->> 'quantity')::int;
    v_l_price := (v_line.item ->> 'unit_price_kobo')::bigint;
    v_l_total := v_l_qty::bigint * v_l_price;
    v_l_storage := coalesce(nullif(v_line.item ->> 'storage_state', ''), 'normal');
    insert into public.biz_sale_lines
      (tenant_id, business_id, branch_id, sale_id, product_id, storage_state,
       quantity, unit_price_kobo, line_total_kobo)
    values (v_tenant, v_business, v_branch, v_sale_id, v_l_product, v_l_storage,
      v_l_qty, v_l_price, v_l_total)
    returning id into v_line_id;
    insert into public.biz_inventory_movements
      (tenant_id, business_id, branch_id, product_id, movement_type,
       storage_state, quantity, source_submission_id,
       recorded_by, reason, idempotency_key)
    values (v_tenant, v_business, v_branch, v_l_product, 'sale',
      v_l_storage, v_l_qty, v_sub,
      v_emp, 'confirmed sale posting',
      'posting:' || v_sub::text || ':sale_movement:' || v_idx::text);
    v_line_ids := v_line_ids || v_line_id;
  end loop;

  -- Embedded payment + custody, only when verified payment details exist.
  if v_pay is not null then
    v_key_pay := 'posting:' || v_sub::text || ':sale_payment';
    insert into public.biz_payments
      (tenant_id, business_id, branch_id, sale_id, amount_kobo, method,
       received_by, destination_account, reference, status, paid_at,
       source_submission_id, idempotency_key)
    values (v_tenant, v_business, v_branch, v_sale_id, v_p_amount, v_p_method,
      v_p_received, v_p_dest, v_p_ref, 'confirmed', v_p_at,
      v_sub, v_key_pay)
    returning id into v_pay_id;
    v_key_cust := 'posting:' || v_sub::text || ':sale_custody';
    insert into public.biz_cash_custody_entries
      (tenant_id, business_id, branch_id, custodian_id, entry_type,
       amount_kobo, related_payment_id, occurred_at, recorded_by,
       notes, idempotency_key)
    values (v_tenant, v_business, v_branch, v_p_received, 'cash_received',
      v_p_amount, v_pay_id, v_p_at, v_emp,
      'confirmed sale payment posting', v_key_cust)
    returning id into v_custody_id;
  end if;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'sale';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'sale', v_sale_id::text,
      jsonb_build_object('sale_id', v_sale_id::text,
        'customer_id', v_customer::text,
        'subtotal_kobo', v_subtotal, 'discount_kobo', v_discount,
        'total_kobo', v_total, 'line_count', v_idx,
        'payment_id', v_pay_id::text),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_sales', v_sale_id::text)
  on conflict do nothing;
  for v_idx in 1 .. coalesce(array_length(v_line_ids, 1), 0) loop
    insert into public.brain_context_links
      (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
    values (v_tenant, v_mem_id, 'derived_from', 'biz_sale_lines', v_line_ids[v_idx]::text)
    on conflict do nothing;
  end loop;
  if v_pay_id is not null then
    insert into public.brain_context_links
      (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
    values (v_tenant, v_mem_id, 'derived_from', 'biz_payments', v_pay_id::text)
    on conflict do nothing;
    insert into public.brain_context_links
      (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
    values (v_tenant, v_mem_id, 'derived_from', 'biz_cash_custody_entries', v_custody_id::text)
    on conflict do nothing;
  end if;

  return jsonb_build_object('status', 'posted', 'posting_type', 'sale',
    'submission_id', v_sub::text, 'sale_id', v_sale_id::text,
    'payment_id', v_pay_id::text, 'cash_custody_entry_id', v_custody_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 3. payment: one confirmed biz_payments row against an existing confirmed
-- sale + the matching append-only cash-custody entry + one verified Brain
-- memory. All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_payment(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_sale uuid; v_amount bigint; v_method text; v_received uuid;
  v_total bigint; v_paid bigint;
  v_dest text; v_ref text; v_paid_at timestamptz;
  v_key_pay text; v_key_cust text;
  v_pay_id uuid; v_custody_id uuid; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['payment']);

  -- Idempotent retry: the payment's deterministic key already exists.
  v_key_pay := 'posting:' || v_sub::text || ':payment';
  select p.id into v_pay_id
    from public.biz_payments p
    where p.tenant_id = v_tenant and p.business_id = v_business
      and p.branch_id = v_branch and p.idempotency_key = v_key_pay;
  if found then
    v_key_cust := 'posting:' || v_sub::text || ':payment_custody';
    select c.id into v_custody_id
      from public.biz_cash_custody_entries c
      where c.tenant_id = v_tenant and c.business_id = v_business
        and c.branch_id = v_branch and c.idempotency_key = v_key_cust;
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'payment';
    -- Retry completeness: payment, custody, memory and both context links
    -- must exist. Anything less fails closed.
    if v_custody_id is null then
      raise exception 'INCOMPLETE: submission % payment posting is missing its cash custody entry', v_sub;
    end if;
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % payment posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_payments'
          and l.target_record_id = v_pay_id::text) then
      raise exception 'INCOMPLETE: submission % payment posting is missing its payment context link', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_cash_custody_entries'
          and l.target_record_id = v_custody_id::text) then
      raise exception 'INCOMPLETE: submission % payment posting is missing its custody context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'payment',
      'submission_id', v_sub::text, 'payment_id', v_pay_id::text,
      'cash_custody_entry_id', v_custody_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified payment block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read (top-level payload
  -- values cannot override anything). A kind label inside verified, when
  -- present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  begin
    v_sale := nullif(v_ver ->> 'sale_id', '')::uuid;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % has invalid sale_id', v_sub;
  end;
  if v_sale is null then
    raise exception 'MALFORMED: submission % verified block requires sale_id', v_sub;
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
  v_dest := nullif(v_ver ->> 'destination_account', '');
  v_ref := nullif(v_ver ->> 'reference', '');
  v_paid_at := now();
  if v_ver ? 'paid_at' and nullif(v_ver ->> 'paid_at', '') is not null then
    begin
      v_paid_at := (v_ver ->> 'paid_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid paid_at', v_sub;
    end;
  end if;

  -- Lock the sale first, then derive the unpaid balance from confirmed
  -- payments only. Pending/reversed payments never count as paid, and
  -- caller values never override: status and balance are calculated here.
  -- Voided (or draft) sales are not payable and fail closed.
  select s.total_kobo into v_total
    from public.biz_sales s
    where s.tenant_id = v_tenant and s.business_id = v_business
      and s.branch_id = v_branch and s.id = v_sale
      and s.status in ('confirmed', 'partially_paid', 'paid')
    for update;
  if not found then
    raise exception 'SCOPE: sale % is not a payable sale of this tenant/business/branch (voided sales never accept payments)', v_sale;
  end if;
  select coalesce(sum(p.amount_kobo), 0) into v_paid
    from public.biz_payments p
    where p.tenant_id = v_tenant and p.business_id = v_business
      and p.branch_id = v_branch and p.sale_id = v_sale
      and p.status = 'confirmed';
  if v_amount > (v_total - v_paid) then
    raise exception 'MALFORMED: submission % payment % exceeds unpaid balance % of sale %',
      v_sub, v_amount, (v_total - v_paid), v_sale;
  end if;
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = v_tenant and e.id = v_received) then
    raise exception 'SCOPE: employee % is not in this tenant', v_received;
  end if;

  insert into public.biz_payments
    (tenant_id, business_id, branch_id, sale_id, amount_kobo, method,
     received_by, destination_account, reference, status, paid_at,
     source_submission_id, idempotency_key)
  values (v_tenant, v_business, v_branch, v_sale, v_amount, v_method,
    v_received, v_dest, v_ref, 'confirmed', v_paid_at,
    v_sub, v_key_pay)
  returning id into v_pay_id;

  -- Confirmed payments atomically move the sale: still owing means
  -- partially_paid, fully covered means paid. The already_posted path above
  -- returns before any insert, so a retried submission never counts twice.
  update public.biz_sales
    set status = case when (v_paid + v_amount) >= v_total then 'paid' else 'partially_paid' end,
      updated_at = now()
    where tenant_id = v_tenant and business_id = v_business
      and branch_id = v_branch and id = v_sale;

  v_key_cust := 'posting:' || v_sub::text || ':payment_custody';
  insert into public.biz_cash_custody_entries
    (tenant_id, business_id, branch_id, custodian_id, entry_type,
     amount_kobo, related_payment_id, occurred_at, recorded_by,
     notes, idempotency_key)
  values (v_tenant, v_business, v_branch, v_received, 'cash_received',
    v_amount, v_pay_id, v_paid_at, v_emp,
    'confirmed payment posting', v_key_cust)
  returning id into v_custody_id;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'payment';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'payment', v_pay_id::text,
      jsonb_build_object('payment_id', v_pay_id::text,
        'sale_id', v_sale::text, 'amount_kobo', v_amount,
        'method', v_method),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_payments', v_pay_id::text)
  on conflict do nothing;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_cash_custody_entries', v_custody_id::text)
  on conflict do nothing;

  return jsonb_build_object('status', 'posted', 'posting_type', 'payment',
    'submission_id', v_sub::text, 'payment_id', v_pay_id::text,
    'cash_custody_entry_id', v_custody_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 4. expense: one confirmed biz_expenses row + (cash payment_method only)
-- the matching append-only cash-custody entry + one verified Brain memory.
-- A referenced approval must already be approved; approval is never bypassed
-- or auto-granted here. All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_expense(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_category text; v_descr text; v_amount bigint; v_method text;
  v_incurred timestamptz; v_paid_by uuid := null; v_recorded_by uuid;
  v_approval uuid := null;
  v_key_exp text; v_key_cust text;
  v_exp_method text;
  v_exp_id uuid; v_custody_id uuid := null; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['expense']);

  -- Idempotent retry: the expense's deterministic key already exists.
  v_key_exp := 'posting:' || v_sub::text || ':expense';
  select e.id into v_exp_id
    from public.biz_expenses e
    where e.tenant_id = v_tenant and e.business_id = v_business
      and e.branch_id = v_branch and e.idempotency_key = v_key_exp;
  if found then
    v_key_cust := 'posting:' || v_sub::text || ':expense_custody';
    select c.id into v_custody_id
      from public.biz_cash_custody_entries c
      where c.tenant_id = v_tenant and c.business_id = v_business
        and c.branch_id = v_branch and c.idempotency_key = v_key_cust;
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'expense';
    -- Retry completeness: the expense plus its memory and expense link must
    -- exist; a cash expense additionally requires its custody entry and
    -- custody link. Anything less fails closed.
    select e.payment_method into v_exp_method
      from public.biz_expenses e
      where e.id = v_exp_id;
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % expense posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_expenses'
          and l.target_record_id = v_exp_id::text) then
      raise exception 'INCOMPLETE: submission % expense posting is missing its expense context link', v_sub;
    end if;
    if v_exp_method = 'cash' and v_custody_id is null then
      raise exception 'INCOMPLETE: submission % cash expense posting is missing its cash custody entry', v_sub;
    end if;
    if v_custody_id is not null
        and not exists (select 1 from public.brain_context_links l
          where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
            and l.relation_type = 'derived_from'
            and l.target_table = 'biz_cash_custody_entries'
            and l.target_record_id = v_custody_id::text) then
      raise exception 'INCOMPLETE: submission % expense posting is missing its custody context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'expense',
      'submission_id', v_sub::text, 'expense_id', v_exp_id::text,
      'cash_custody_entry_id', v_custody_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified expense block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read (top-level payload
  -- values cannot override anything). A kind label inside verified, when
  -- present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  v_category := v_ver ->> 'category';
  if v_category is null or v_category not in ('fuel', 'maintenance', 'salaries',
      'transport', 'packaging', 'utilities', 'rent', 'purchases', 'other') then
    raise exception 'MALFORMED: submission % verified block requires a valid category', v_sub;
  end if;
  v_descr := nullif(btrim(v_ver ->> 'description'), '');
  if v_descr is null then
    raise exception 'MALFORMED: submission % verified block requires a non-empty description', v_sub;
  end if;
  begin
    v_amount := (v_ver ->> 'amount_kobo')::bigint;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end;
  if v_amount is null or v_amount <= 0 then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end if;
  v_method := coalesce(nullif(v_ver ->> 'payment_method', ''), 'cash');
  if v_method not in ('cash', 'transfer', 'pos', 'other') then
    raise exception 'MALFORMED: submission % verified block has invalid payment_method', v_sub;
  end if;
  v_incurred := now();
  if v_ver ? 'incurred_at' and nullif(v_ver ->> 'incurred_at', '') is not null then
    begin
      v_incurred := (v_ver ->> 'incurred_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid incurred_at', v_sub;
    end;
  end if;
  if v_ver ? 'paid_by' and nullif(v_ver ->> 'paid_by', '') is not null then
    begin
      v_paid_by := (v_ver ->> 'paid_by')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid paid_by', v_sub;
    end;
    if not exists (select 1 from public.biz_employees e
        where e.tenant_id = v_tenant and e.id = v_paid_by) then
      raise exception 'SCOPE: employee % is not in this tenant', v_paid_by;
    end if;
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

  -- Approval gate: a referenced request must belong to this tenant AND
  -- already be approved. Anything else fails closed -- approval is never
  -- bypassed, invented, or auto-granted by this layer.
  if v_ver ? 'approval_request_id'
      and nullif(v_ver ->> 'approval_request_id', '') is not null then
    begin
      v_approval := (v_ver ->> 'approval_request_id')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: submission % verified block has invalid approval_request_id', v_sub;
    end;
    if not exists (select 1 from public.biz_approval_requests a
        where a.tenant_id = v_tenant and a.id = v_approval
          and a.status = 'approved') then
      raise exception 'APPROVAL: approval request % is not approved for this tenant', v_approval;
    end if;
  end if;

  insert into public.biz_expenses
    (tenant_id, business_id, branch_id, category, description, amount_kobo,
     payment_method, incurred_at, paid_by, recorded_by, source_submission_id,
     status, approval_request_id, idempotency_key, confirmed_by, confirmed_at)
  values (v_tenant, v_business, v_branch, v_category, v_descr, v_amount,
    v_method, v_incurred, v_paid_by, v_recorded_by, v_sub,
    'confirmed', v_approval, v_key_exp, v_emp, now())
  returning id into v_exp_id;

  -- Custody applies to cash expenses only: cash left the custodian's hand.
  -- Non-cash methods (transfer/pos/other) post no custody entry.
  if v_method = 'cash' then
    v_key_cust := 'posting:' || v_sub::text || ':expense_custody';
    insert into public.biz_cash_custody_entries
      (tenant_id, business_id, branch_id, custodian_id, entry_type,
       amount_kobo, related_expense_id, occurred_at, recorded_by,
       notes, idempotency_key)
    values (v_tenant, v_business, v_branch,
      coalesce(v_paid_by, v_recorded_by), 'expense_paid',
      v_amount, v_exp_id, v_incurred, v_recorded_by,
      'confirmed expense posting', v_key_cust)
    returning id into v_custody_id;
  end if;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  -- Amount/category/description only: never secrets, never personal data
  -- beyond the scoped employee reference already on the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'expense';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'expense', v_exp_id::text,
      jsonb_build_object('expense_id', v_exp_id::text,
        'category', v_category, 'description', v_descr,
        'amount_kobo', v_amount, 'payment_method', v_method),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_expenses', v_exp_id::text)
  on conflict do nothing;
  if v_custody_id is not null then
    insert into public.brain_context_links
      (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
    values (v_tenant, v_mem_id, 'derived_from', 'biz_cash_custody_entries', v_custody_id::text)
    on conflict do nothing;
  end if;

  return jsonb_build_object('status', 'posted', 'posting_type', 'expense',
    'submission_id', v_sub::text, 'expense_id', v_exp_id::text,
    'cash_custody_entry_id', v_custody_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- Least privilege: only service_role may execute the posting API. Frontend
-- roles fail closed; direct anon/authenticated execution is denied.
-- ---------------------------------------------------------------------------
-- The lock helper is internal only: even service_role may not call it
-- directly. Only postgres/owner retains EXECUTE; the four SECURITY DEFINER
-- posting RPCs invoke it internally (definer rights, ownership chain), which
-- needs no caller grant -- verified by local probes.
revoke execute on function public._amose_lock_confirmed_submission(uuid, text[])
  from public, anon, authenticated, service_role;
revoke execute on function public.amose_post_production(uuid)
  from public, anon, authenticated;
revoke execute on function public.amose_post_sale(uuid)
  from public, anon, authenticated;
revoke execute on function public.amose_post_payment(uuid)
  from public, anon, authenticated;
revoke execute on function public.amose_post_expense(uuid)
  from public, anon, authenticated;

grant execute on function public.amose_post_production(uuid)
  to service_role;
grant execute on function public.amose_post_sale(uuid)
  to service_role;
grant execute on function public.amose_post_payment(uuid)
  to service_role;
grant execute on function public.amose_post_expense(uuid)
  to service_role;

commit;
