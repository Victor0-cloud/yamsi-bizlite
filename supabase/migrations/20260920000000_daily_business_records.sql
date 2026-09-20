-- Stage 1: complete daily business records for YAMSI BizLite.
--
-- Forward-only migration (applied migrations are never edited). Schema +
-- code only: no data rows are seeded and no business-policy VALUES are
-- inserted. Money stays integer kobo (bigint); floats are never used.
--
-- What changes and why:
--   1. Expense categories grow from 9 to 12 machine values. The new
--      values (task_force, atwap_dues, tricycle_service) cover task-force
--      payments, ATWAP dues, and tricycle service; salaries already covers
--      operator pay and other already covers miscellaneous. Staff wording
--      is mapped deterministically in water_intake.py; the staff
--      description is always preserved on the expense row.
--   2. biz_cash_custody_entries gains three nullable columns so the
--      append-only trail retains full handover/deposit facts:
--        from_custodian_id  (cash_handed_over: who handed the cash over;
--          NULL for every other entry type)
--        destination_account (bank_deposit: approved destination account)
--        deposit_reference   (bank_deposit: deposit reference/evidence)
--      A CHECK rejects self-handover rows (from == custodian) at the
--      database level, behind the RPC checks.
--   3. Two new transactional, idempotent, service-role-only posting RPCs
--      following the Phase 2 operational-posting architecture exactly
--      (single-uuid argument, locked confirmed submission, deterministic
--      'posting:<submission_id>:<role>' keys, retry-completeness checks,
--      verified Brain memory + context links in the same transaction):
--        public.amose_post_cash_handover(uuid)  -> posting_type cash_handover
--        public.amose_post_bank_deposit(uuid)   -> posting_type bank_deposit
--      Both resolve employees from authoritative scoped rows (tenant +
--      branch assignment), never from message text. The deposit RPC
--      additionally refuses a second confirmed entry reusing an already
--      recorded (destination account, reference) pair in the same scope
--      (CONFLICT, plus a partial unique backstop index), so one bank
--      deposit can never post twice under two submissions.
--   4. _amose_validate_verified, amose_confirm_submission,
--      amose_queue_review_requests, and
--      amose_queue_telegram_review_requests learn the two new kinds; the
--      audit posting_type CHECK accepts them. Chat confirm stays
--      sale-only; handover/deposit drafts need API review.
--   5. _amose_review_request_text covers the new kinds (business/branch,
--      record type, amount, custodian names, destination account and
--      reference, missing/unresolved fields -- never UUIDs) and renders
--      parsed payment/expense/handover/deposit amount_kobo with
--      _amose_format_kobo: parsed intake stores genuine integer kobo in
--      those fields, for which format_kobo is exact. (The earlier
--      format_naira call sites were written before any payment/expense
--      extractor existed, so no stored draft depends on them.) Sale
--      unit_price stays naira via _amose_format_naira, byte-identical.
--
-- Least privilege preserved: new RPCs are revoked from PUBLIC/anon/
-- authenticated and granted to service_role only. Internal helpers stay
-- owner-only (CREATE OR REPLACE preserves existing revokes). RLS,
-- sender-resolution, and review-reference rules are untouched.
begin;

-- ---------------------------------------------------------------------------
-- 1. Expense categories: 9 -> 12 stable machine values.
-- ---------------------------------------------------------------------------
alter table public.biz_expenses
  drop constraint if exists biz_expenses_category_check;
alter table public.biz_expenses
  add constraint biz_expenses_category_check
  check (category in ('fuel', 'maintenance', 'salaries', 'transport',
    'packaging', 'utilities', 'rent', 'purchases', 'other',
    'task_force', 'atwap_dues', 'tricycle_service'));

-- ---------------------------------------------------------------------------
-- 2. Custody trail columns for handovers and deposits (all nullable, so
-- existing rows are unaffected). Corrections stay new 'correction' rows.
-- ---------------------------------------------------------------------------
alter table public.biz_cash_custody_entries
  add column if not exists from_custodian_id uuid;
alter table public.biz_cash_custody_entries
  add column if not exists destination_account text;
alter table public.biz_cash_custody_entries
  add column if not exists deposit_reference text;

alter table public.biz_cash_custody_entries
  drop constraint if exists biz_cash_custody_from_custodian_fk;
alter table public.biz_cash_custody_entries
  add constraint biz_cash_custody_from_custodian_fk
  foreign key (tenant_id, from_custodian_id)
  references public.biz_employees(tenant_id, id);

alter table public.biz_cash_custody_entries
  drop constraint if exists biz_cash_custody_no_self_handover;
alter table public.biz_cash_custody_entries
  add constraint biz_cash_custody_no_self_handover
  check (from_custodian_id is null
    or from_custodian_id is distinct from custodian_id);

comment on column public.biz_cash_custody_entries.from_custodian_id is
  'cash_handed_over only: the employee who handed the cash over. The receiver stays in custodian_id.';
comment on column public.biz_cash_custody_entries.destination_account is
  'bank_deposit only: the approved destination account named at confirmation.';
comment on column public.biz_cash_custody_entries.deposit_reference is
  'bank_deposit only: the deposit reference/evidence required at confirmation.';

-- Backstop for the deposit-duplicate rule: at most one confirmed entry
-- per (tenant, business, branch, destination account, reference),
-- compared case-insensitively after trimming so trivial variants cannot
-- bypass it. Partial so existing rows (all NULL references) are
-- unaffected, and NULLs never collide with each other.
create unique index if not exists biz_cash_custody_deposit_ref_unique
  on public.biz_cash_custody_entries
    (tenant_id, business_id, branch_id,
     (lower(btrim(destination_account))), (lower(btrim(deposit_reference))))
  where deposit_reference is not null;

-- ---------------------------------------------------------------------------
-- 4. Verified-block validation learns the 12 expense categories and the
-- two new posting types. Scope/existence checks stay with the posting
-- RPCs, which re-validate authoritatively inside the same transaction.
-- Owner-only; never granted to any caller role.
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
        'transport', 'packaging', 'utilities', 'rent', 'purchases', 'other',
        'task_force', 'atwap_dues', 'tricycle_service') then
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

  elsif p_posting_type = 'cash_handover' then
    -- Both custodians are authoritative employee UUIDs resolved from
    -- scoped database records at review time; raw message names are never
    -- accepted here (they are not UUIDs, so they fail closed below).
    begin
      v_tmp_uuid := nullif(p_verified ->> 'from_employee_id', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires from_employee_id';
    end;
    if v_tmp_uuid is null then
      raise exception 'MALFORMED: verified block requires from_employee_id';
    end if;
    begin
      v_tmp_uuid := nullif(p_verified ->> 'to_employee_id', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires to_employee_id';
    end;
    if v_tmp_uuid is null then
      raise exception 'MALFORMED: verified block requires to_employee_id';
    end if;
    if nullif(p_verified ->> 'from_employee_id', '')
        is not distinct from nullif(p_verified ->> 'to_employee_id', '') then
      raise exception 'MALFORMED: handover parties must differ; self-handover is not allowed';
    end if;
    begin
      v_tmp_bigint := (p_verified ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end;
    if v_tmp_bigint is null or v_tmp_bigint <= 0 then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end if;
    if p_verified ? 'handed_at' and nullif(p_verified ->> 'handed_at', '') is not null then
      begin
        perform (p_verified ->> 'handed_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified block has invalid handed_at';
      end;
    end if;
    if p_verified ? 'recorded_by' and nullif(p_verified ->> 'recorded_by', '') is not null then
      begin
        v_tmp_uuid := (p_verified ->> 'recorded_by')::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: verified block has invalid recorded_by';
      end;
    end if;

  elsif p_posting_type = 'bank_deposit' then
    -- The depositor is an authoritative employee UUID; amount, approved
    -- destination account, and deposit reference are all required. A
    -- deposit is never confirmed without a reference.
    begin
      v_tmp_uuid := nullif(p_verified ->> 'deposited_by', '')::uuid;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires deposited_by';
    end;
    if v_tmp_uuid is null then
      raise exception 'MALFORMED: verified block requires deposited_by';
    end if;
    begin
      v_tmp_bigint := (p_verified ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end;
    if v_tmp_bigint is null or v_tmp_bigint <= 0 then
      raise exception 'MALFORMED: verified block requires amount_kobo > 0';
    end if;
    if nullif(btrim(p_verified ->> 'destination_account'), '') is null then
      raise exception 'MALFORMED: verified block requires a non-empty destination_account';
    end if;
    if nullif(btrim(p_verified ->> 'reference'), '') is null then
      raise exception 'MALFORMED: verified block requires a non-empty reference';
    end if;
    if p_verified ? 'deposited_at' and nullif(p_verified ->> 'deposited_at', '') is not null then
      begin
        perform (p_verified ->> 'deposited_at')::timestamptz;
      exception when others then
        raise exception 'MALFORMED: verified block has invalid deposited_at';
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

-- ---------------------------------------------------------------------------
-- 5. Cash-handover posting: one confirmed custody handover row (receiver in
-- custodian_id, giver in from_custodian_id) + one verified Brain memory.
-- Both employees resolve from authoritative scoped rows: they must exist
-- in the submission's tenant AND hold an assignment in its business and
-- branch, so cross-tenant/cross-business/cross-branch handovers fail
-- closed. Self-handover fails closed. All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_cash_handover(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_from uuid; v_to uuid; v_amount bigint;
  v_occurred timestamptz; v_recorded_by uuid;
  v_key_cust text;
  v_custody_id uuid; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['cash_handover']);

  -- Idempotent retry: the handover's deterministic key already exists.
  v_key_cust := 'posting:' || v_sub::text || ':cash_handover';
  select c.id into v_custody_id
    from public.biz_cash_custody_entries c
    where c.tenant_id = v_tenant and c.business_id = v_business
      and c.branch_id = v_branch and c.idempotency_key = v_key_cust;
  if found then
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'cash_handover';
    -- Retry completeness: custody, memory, and custody link must exist.
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % cash handover posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_cash_custody_entries'
          and l.target_record_id = v_custody_id::text) then
      raise exception 'INCOMPLETE: submission % cash handover posting is missing its custody context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'cash_handover',
      'submission_id', v_sub::text, 'cash_custody_entry_id', v_custody_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified cash handover block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read. A kind label inside
  -- verified, when present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  begin
    v_from := nullif(v_ver ->> 'from_employee_id', '')::uuid;
    v_to := nullif(v_ver ->> 'to_employee_id', '')::uuid;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires from_employee_id and to_employee_id', v_sub;
  end;
  if v_from is null or v_to is null then
    raise exception 'MALFORMED: submission % verified block requires from_employee_id and to_employee_id', v_sub;
  end if;
  if v_from = v_to then
    raise exception 'MALFORMED: submission % handover parties must differ; self-handover is not allowed', v_sub;
  end if;
  -- Authoritative scoped resolution: both custodians must belong to this
  -- tenant AND be assigned to this business/branch. Arbitrary UUIDs from
  -- message text fail here (they match no scoped row).
  if not exists (select 1 from public.biz_assignments a
      where a.tenant_id = v_tenant and a.employee_id = v_from
        and a.business_id = v_business and a.branch_id = v_branch) then
    raise exception 'SCOPE: handover giver % is not assigned to %/% in this tenant', v_from, v_business, v_branch;
  end if;
  if not exists (select 1 from public.biz_assignments a
      where a.tenant_id = v_tenant and a.employee_id = v_to
        and a.business_id = v_business and a.branch_id = v_branch) then
    raise exception 'SCOPE: handover receiver % is not assigned to %/% in this tenant', v_to, v_business, v_branch;
  end if;
  begin
    v_amount := (v_ver ->> 'amount_kobo')::bigint;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end;
  if v_amount is null or v_amount <= 0 then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end if;
  v_occurred := now();
  if v_ver ? 'handed_at' and nullif(v_ver ->> 'handed_at', '') is not null then
    begin
      v_occurred := (v_ver ->> 'handed_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid handed_at', v_sub;
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

  insert into public.biz_cash_custody_entries
    (tenant_id, business_id, branch_id, custodian_id, from_custodian_id, entry_type,
     amount_kobo, occurred_at, recorded_by, notes, idempotency_key)
  values (v_tenant, v_business, v_branch, v_to, v_from, 'cash_handed_over',
    v_amount, v_occurred, v_recorded_by,
    'confirmed cash handover posting', v_key_cust)
  returning id into v_custody_id;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'cash_handover';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'cash_handover', v_custody_id::text,
      jsonb_build_object('cash_custody_entry_id', v_custody_id::text,
        'from_employee_id', v_from::text, 'to_employee_id', v_to::text,
        'amount_kobo', v_amount),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_cash_custody_entries', v_custody_id::text)
  on conflict do nothing;

  return jsonb_build_object('status', 'posted', 'posting_type', 'cash_handover',
    'submission_id', v_sub::text, 'cash_custody_entry_id', v_custody_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 6. Bank-deposit posting: one confirmed deposit_confirmed custody entry +
-- one verified Brain memory. The depositor resolves from authoritative
-- scoped rows; amount, owner-approved destination account, and reference
-- are required. Approval comes from the owner-confirmed
-- biz_setting_versions key 'approved_bank_deposit_accounts' for this
-- exact tenant/business/branch ({"accounts": [{"name": ..., "reference"?}]});
-- matching is case-insensitive after trimming, the canonical approved
-- name is stored, and anything absent/malformed/unapproved fails closed --
-- raw text alone can never confirm a deposit because this RPC only runs
-- inside the human-confirmation transaction on a verified block. A deposit
-- reference already recorded for the same destination account in this
-- scope under a different submission fails closed as CONFLICT (plus a
-- partial unique backstop index), so retries are idempotent and
-- double-reports of one bank deposit can never post twice.
-- All-or-nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_post_bank_deposit(p_submission_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_emp uuid;
  v_kind text; v_payload jsonb;
  v_ver jsonb;
  v_depositor uuid; v_amount bigint; v_dest text; v_ref text;
  v_setting jsonb; v_acct jsonb; v_canonical_dest text;
  v_occurred timestamptz; v_recorded_by uuid;
  v_key_cust text;
  v_custody_id uuid; v_mem_id uuid;
begin
  select * into v_tenant, v_business, v_branch, v_sub, v_emp, v_kind, v_payload
    from public._amose_lock_confirmed_submission(
      p_submission_id, array['bank_deposit']);

  -- Idempotent retry: the deposit's deterministic key already exists.
  v_key_cust := 'posting:' || v_sub::text || ':bank_deposit';
  select c.id into v_custody_id
    from public.biz_cash_custody_entries c
    where c.tenant_id = v_tenant and c.business_id = v_business
      and c.branch_id = v_branch and c.idempotency_key = v_key_cust;
  if found then
    select mem.id into v_mem_id
      from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
        and mem.subject_type = 'bank_deposit';
    -- Retry completeness: custody, memory, and custody link must exist.
    if v_mem_id is null then
      raise exception 'INCOMPLETE: submission % bank deposit posting is missing its brain memory', v_sub;
    end if;
    if not exists (select 1 from public.brain_context_links l
        where l.tenant_id = v_tenant and l.from_memory_id = v_mem_id
          and l.relation_type = 'derived_from'
          and l.target_table = 'biz_cash_custody_entries'
          and l.target_record_id = v_custody_id::text) then
      raise exception 'INCOMPLETE: submission % bank deposit posting is missing its custody context link', v_sub;
    end if;
    return jsonb_build_object('status', 'already_posted', 'posting_type', 'bank_deposit',
      'submission_id', v_sub::text, 'cash_custody_entry_id', v_custody_id::text,
      'brain_memory_id', v_mem_id::text, 'is_retry', true);
  end if;

  v_ver := v_payload -> 'verified';
  if v_ver is null or jsonb_typeof(v_ver) <> 'object' then
    raise exception 'MALFORMED: submission % has no verified bank deposit block', v_sub;
  end if;
  -- Boundary: only the verified object is ever read. A kind label inside
  -- verified, when present, must agree with the submission kind.
  if v_ver ? 'kind' and nullif(v_ver ->> 'kind', '') is not null
      and (v_ver ->> 'kind') is distinct from v_kind then
    raise exception 'MALFORMED: submission % verified kind % does not match submission kind %',
      v_sub, v_ver ->> 'kind', v_kind;
  end if;
  begin
    v_depositor := nullif(v_ver ->> 'deposited_by', '')::uuid;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires deposited_by', v_sub;
  end;
  if v_depositor is null then
    raise exception 'MALFORMED: submission % verified block requires deposited_by', v_sub;
  end if;
  -- Authoritative scoped resolution: the depositor must belong to this
  -- tenant AND be assigned to this business/branch.
  if not exists (select 1 from public.biz_assignments a
      where a.tenant_id = v_tenant and a.employee_id = v_depositor
        and a.business_id = v_business and a.branch_id = v_branch) then
    raise exception 'SCOPE: depositor % is not assigned to %/% in this tenant', v_depositor, v_business, v_branch;
  end if;
  begin
    v_amount := (v_ver ->> 'amount_kobo')::bigint;
  exception when invalid_text_representation then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end;
  if v_amount is null or v_amount <= 0 then
    raise exception 'MALFORMED: submission % verified block requires amount_kobo > 0', v_sub;
  end if;
  v_dest := nullif(btrim(v_ver ->> 'destination_account'), '');
  if v_dest is null then
    raise exception 'MALFORMED: submission % verified block requires a non-empty destination_account', v_sub;
  end if;
  v_ref := nullif(btrim(v_ver ->> 'reference'), '');
  if v_ref is null then
    raise exception 'MALFORMED: submission % verified block requires a non-empty reference', v_sub;
  end if;
  -- Owner-approved destination account: the latest effective
  -- 'approved_bank_deposit_accounts' setting for this exact
  -- tenant/business/branch must exist, be well-formed
  -- ({"accounts": [{"name": ..., "reference"?: ...}, ...]} with at least
  -- one named account), and contain the supplied destination, compared
  -- case-insensitively after trimming whitespace. The custody record
  -- keeps the canonical approved name. Anything else fails closed: an
  -- account is never trusted merely because it appeared in message text
  -- or the verified block.
  select s.value into v_setting
    from public.biz_setting_versions s
    where s.tenant_id = v_tenant and s.business_id = v_business
      and s.branch_id = v_branch
      and s.key = 'approved_bank_deposit_accounts'
      and s.effective_from <= now()
    order by s.effective_from desc
    limit 1;
  if not found
      or v_setting is null or jsonb_typeof(v_setting) <> 'object'
      or jsonb_typeof(v_setting -> 'accounts') <> 'array'
      or jsonb_array_length(v_setting -> 'accounts') < 1 then
    raise exception 'APPROVAL: no approved bank deposit accounts are configured for %/%', v_business, v_branch;
  end if;
  v_canonical_dest := null;
  for v_acct in select value from jsonb_array_elements(v_setting -> 'accounts') as value loop
    if jsonb_typeof(v_acct) = 'object'
        and nullif(btrim(v_acct ->> 'name'), '') is not null
        and btrim(lower(v_acct ->> 'name')) = btrim(lower(v_dest)) then
      v_canonical_dest := btrim(v_acct ->> 'name');
      exit;
    end if;
  end loop;
  if v_canonical_dest is null then
    raise exception 'APPROVAL: destination account % is not an approved bank deposit account for %/%',
      v_dest, v_business, v_branch;
  end if;
  v_dest := v_canonical_dest;
  -- Duplicate prevention: this reference is already confirmed for this
  -- destination account in this scope under a different submission (a
  -- double-report of one deposit). Both sides are compared
  -- case-insensitively after trimming, so trivial variants cannot
  -- bypass detection. Scoped by tenant, business, branch, AND
  -- destination account.
  if exists (select 1 from public.biz_cash_custody_entries c
      where c.tenant_id = v_tenant and c.business_id = v_business
        and c.branch_id = v_branch
        and btrim(lower(c.destination_account)) = btrim(lower(v_dest))
        and btrim(lower(c.deposit_reference)) = btrim(lower(v_ref))
        and c.idempotency_key is distinct from v_key_cust) then
    raise exception 'CONFLICT: deposit reference % is already recorded for % in %/%', v_ref, v_dest, v_business, v_branch;
  end if;
  v_occurred := now();
  if v_ver ? 'deposited_at' and nullif(v_ver ->> 'deposited_at', '') is not null then
    begin
      v_occurred := (v_ver ->> 'deposited_at')::timestamptz;
    exception when others then
      raise exception 'MALFORMED: submission % verified block has invalid deposited_at', v_sub;
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

  insert into public.biz_cash_custody_entries
    (tenant_id, business_id, branch_id, custodian_id, entry_type,
     amount_kobo, destination_account, deposit_reference, occurred_at,
     recorded_by, notes, idempotency_key)
  values (v_tenant, v_business, v_branch, v_depositor, 'deposit_confirmed',
    v_amount, v_dest, v_ref, v_occurred, v_recorded_by,
    'confirmed bank deposit posting', v_key_cust)
  returning id into v_custody_id;

  -- Contextual Brain memory: verified facts only, traced to the submission.
  select mem.id into v_mem_id
    from public.brain_memories mem
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.subject_type = 'bank_deposit';
  if not found then
    insert into public.brain_memories
      (tenant_id, business_id, branch_id, employee_id, memory_type,
       subject_type, subject_id, content, source_type,
       source_table, source_record_id, verification_status, confidence,
       created_by_type)
    values (v_tenant, v_business, v_branch, v_emp, 'fact',
      'bank_deposit', v_custody_id::text,
      jsonb_build_object('cash_custody_entry_id', v_custody_id::text,
        'deposited_by', v_depositor::text,
        'amount_kobo', v_amount, 'destination_account', v_dest,
        'reference', v_ref),
      'submission',
      'biz_submissions', v_sub::text, 'verified', 1.00,
      'system')
    returning id into v_mem_id;
  end if;
  insert into public.brain_context_links
    (tenant_id, from_memory_id, relation_type, target_table, target_record_id)
  values (v_tenant, v_mem_id, 'derived_from', 'biz_cash_custody_entries', v_custody_id::text)
  on conflict do nothing;

  return jsonb_build_object('status', 'posted', 'posting_type', 'bank_deposit',
    'submission_id', v_sub::text, 'cash_custody_entry_id', v_custody_id::text,
    'brain_memory_id', v_mem_id::text, 'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 7. Least privilege for the new posting API: only service_role may
-- execute. Frontend roles fail closed; direct anon/authenticated execution
-- is denied.
-- ---------------------------------------------------------------------------
revoke execute on function public.amose_post_cash_handover(uuid)
  from public, anon, authenticated;
revoke execute on function public.amose_post_bank_deposit(uuid)
  from public, anon, authenticated;

grant execute on function public.amose_post_cash_handover(uuid)
  to service_role;
grant execute on function public.amose_post_bank_deposit(uuid)
  to service_role;

-- ---------------------------------------------------------------------------
-- 8. Confirmation dispatch learns the two new kinds. Every other line of
-- the confirm transaction (reviewer resolution, authorization, retry
-- handling, ack routing, audit, case lifecycle) is byte-identical to the
-- Phase 3 version; only the kind -> posting-type mapping and the posting
-- call branch grow. Chat confirm stays sale-only.
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
  elsif v_posting = 'cash_handover' then
    v_post := public.amose_post_cash_handover(v_sub);
  elsif v_posting = 'bank_deposit' then
    v_post := public.amose_post_bank_deposit(v_sub);
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
-- 9. Review-request text covers the new kinds. Every message still shows
-- business/branch, record type, amount/quantity, custodian names, and the
-- destination account + reference for deposits, plus missing/unresolved
-- fields for every kind -- never internal UUIDs. Parsed payment/expense/
-- handover/deposit amount_kobo values are genuine integer kobo, so they
-- render with _amose_format_kobo (exact); sale unit_price stays naira via
-- _amose_format_naira. CREATE OR REPLACE preserves the existing revokes.
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
  else
    v_lines := 'Report proposal (' || pg_catalog.left(p_kind, 40) || ')';
    if v_missing is not null then
      v_lines := v_lines || '. ' || v_missing;
    end if;
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
-- 3. Review-audit posting types accept the two new posting types.
-- ---------------------------------------------------------------------------
alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_posting_type_check;
alter table public.biz_review_audit
  add constraint biz_review_audit_posting_type_check
  check (posting_type is null
    or posting_type in ('production', 'sale', 'payment', 'expense',
      'cash_handover', 'bank_deposit'));

-- ---------------------------------------------------------------------------
-- 10. Review-request queueing accepts the two new kinds on both channels.
-- Bodies are byte-identical transplants of the existing queue functions
-- with only the reviewable-kind gate extended; reference issuance,
-- authorization, separation of duties, routing, and idempotency are
-- unchanged. CREATE OR REPLACE preserves the existing service_role-only
-- grants.
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
      'payment', 'expense', 'cash_handover', 'bank_deposit') then
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
      'payment', 'expense', 'cash_handover', 'bank_deposit') then
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

commit;
