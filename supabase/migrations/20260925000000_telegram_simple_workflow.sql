-- Telegram simple workflow: plain language, guided decisions, money
-- done right, and audited reversal of confirmed transactions.
--
-- Forward-only migration (applied migrations are never edited). Schema +
-- code only: no data rows are seeded, altered, or deleted; no credentials
-- or phone numbers are stored here. No production submission id, review
-- reference, or business fact appears anywhere below.
--
-- What this migration changes and why:
--   1. Money done right (section 1): parsed sale unit_price is NAIRA
--      (message_processor/water_intake store float naira, e.g. 450.0 for
--      NGN 450), but the chat-confirm builder stored it raw into the
--      integer-kobo verified field, posting every confirmed sale at
--      1/100th of its real value. The sale branch now converts
--      naira -> kobo (x100) with a range guard that keeps the product
--      inside bigint. amount_kobo fields (payment/expense/deposit/
--      handover/customer/stock paths) were already true kobo end to
--      end and are untouched.
--   2. Simple reviewer/staff texts (sections 2-4): Telegram review
--      requests, approval/rejection acknowledgements, and the short
--      user-facing code are rendered in plain market language with no
--      UUIDs, keys, or database terms. WhatsApp texts are byte-identical
--      (untouched functions).
--   3. Guided Telegram decisions (section 5): opaque button-token flow
--      state plus three small service-role RPCs (mint/consume/stage)
--      drive Correct (field menu -> value -> preview -> save/back),
--      Reject (reason prompt), and branch-choice buttons. Every press
--      re-checks linked identity, reviewer authorization, scope,
--      self-review prevention, expiry, and single-use inside the
--      database; the review itself still runs only in the Phase 3/4
--      RPCs with stable idempotent request keys.
--   4. Audited reversal (section 6): 'reversed' joins the submission,
--      case, and audit state machines; biz_review_reversals records one
--      row per reversed submission (double reversal impossible; request
--      keys idempotent); amose_reverse_confirmed_submission voids or
--      reverses every operational posting with the tables' native
--      audited terminal states plus offsetting append-only rows, and
--      never updates or deletes original evidence.
begin;

-- ---------------------------------------------------------------------------
-- 1. Chat-confirm sale branch converts naira to kobo. The body below is
-- the migration-23 function verbatim except the two marked sale lines:
-- parsed unit_price is whole NAIRA, so unit_price_kobo is now
-- (naira x 100), guarded to stay inside bigint. All other kinds,
-- corrections, authorization, idempotency, and grants are unchanged.
-- ---------------------------------------------------------------------------
create or replace function public.amose_review_confirm_command(
  p_review_ref text,
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_request_key text,
  p_correction_reason text default null,
  p_corrections jsonb default null
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
  v_corrections jsonb;
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

  -- Real corrections: allowlisted textual overrides applied to the
  -- parsed fields BEFORE the kind builders run, so a corrected value
  -- flows through the same casts and checks as reported text.
  -- Corrections require a reason (the audit invariant that
  -- corrections explain themselves is unchanged).
  v_corrections := public._amose_apply_intake_corrections(
    v_kind, v_fields, p_corrections);
  if v_corrections <> '{}'::jsonb then
    if v_reason is null then
      raise exception 'MALFORMED: correction values require a correction reason';
    end if;
    v_fields := v_fields || v_corrections;
  end if;

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
    -- MONEY FIX: v_price is whole NAIRA (e.g. 450 for NGN 450); the
    -- verified/posted field is integer KOBO, so convert (x100). The
    -- bound keeps naira x 100 inside bigint.
    if v_price < 0 or v_price > 92233720368547758 then
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
        'unit_price_kobo', (v_price * 100)::bigint,
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

-- CREATE OR REPLACE preserves the service-role-only grant; restated
-- here so the matrix stays explicit.
revoke execute on function public.amose_review_confirm_command(
  text, text, text, text, text, jsonb)
  from public, anon, authenticated;
grant execute on function public.amose_review_confirm_command(
  text, text, text, text, text, jsonb)
  to service_role;

-- ---------------------------------------------------------------------------
-- 2. Plain-language Telegram texts. Display-only helpers (owner-only, like
-- _amose_format_naira): no UUIDs, keys, or database terms in any output.
-- WhatsApp text functions are untouched.
-- ---------------------------------------------------------------------------
create or replace function public._amose_short_code(p_review_ref text)
returns text
language sql
immutable
security definer
set search_path = pg_catalog
as $func$
  select substring(nullif(btrim(p_review_ref), '') from 4 for 4);
$func$;

revoke execute on function public._amose_short_code(text)
  from public, anon, authenticated, service_role;

-- Short human reference only: the first 4 characters of the review
-- body's alphabet (e.g. YR-KNVYZPVBKQ -> KNVY). Full immutable IDs and
-- audit records stay in the database; never treat the code as a key.
comment on function public._amose_short_code(text) is
  'User-facing 4-character reference derived from a review reference. Display only; never a lookup key.';

create or replace function public._amose_telegram_review_text(
  p_tenant uuid,
  p_business text,
  p_branch text,
  p_kind text,
  p_parsed jsonb,
  p_reporter text
)
returns text
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_fields jsonb;
  v_reporter text;
  v_biz text;
  v_branch text;
  v_qty numeric; v_unit text; v_price numeric; v_method text;
  v_amount numeric; v_line text := '';
  v_total text := '';
begin
  v_fields := case when jsonb_typeof(p_parsed -> 'fields') = 'object'
    then p_parsed -> 'fields' else '{}'::jsonb end;
  v_reporter := nullif(btrim(p_reporter), '');
  if v_reporter is null then
    v_reporter := 'Staff';
  end if;
  v_reporter := pg_catalog.left(v_reporter, 40);
  select nullif(btrim(b.name), '') into v_biz
    from public.biz_businesses b
    where b.tenant_id = p_tenant and b.id = p_business;
  if v_biz is null then
    v_biz := pg_catalog.left(p_business, 40);
  end if;
  select nullif(btrim(b.name), '') into v_branch
    from public.biz_branches b
    where b.tenant_id = p_tenant and b.business_id = p_business
      and b.id = p_branch;
  if v_branch is null then
    v_branch := pg_catalog.left(p_branch, 40);
  end if;

  if p_kind = 'sale' then
    begin
      v_qty := (v_fields ->> 'quantity')::numeric;
      v_price := (v_fields ->> 'unit_price')::numeric;
    exception when others then
      v_qty := null; v_price := null;
    end;
    v_unit := nullif(btrim(v_fields ->> 'unit'), '');
    if v_unit is null then
      v_unit := 'units';
    end if;
    v_unit := pg_catalog.left(v_unit, 16);
    v_method := nullif(btrim(v_fields ->> 'payment_method'), '');
    if v_qty is not null and v_qty = trunc(v_qty) and v_qty >= 0 then
      if v_qty = 1 and pg_catalog.right(v_unit, 1) = 's' then
        v_unit := pg_catalog.left(v_unit,
          pg_catalog.char_length(v_unit) - 1);
      elsif v_qty <> 1 and pg_catalog.right(v_unit, 1) <> 's' then
        v_unit := v_unit || 's';
      end if;
      v_line := v_reporter || ' sold ' || v_qty::bigint::text || ' '
        || v_unit;
    else
      v_line := v_reporter || ' sold goods';
    end if;
    if v_price is not null then
      v_line := v_line || ' for '
        || coalesce(public._amose_format_naira(v_price), '?');
      if v_method is not null then
        v_line := v_line || ' ' || pg_catalog.left(v_method, 12);
      end if;
      v_line := v_line || '.';
      if v_qty is not null then
        v_total := pg_catalog.chr(10) || 'Total: '
          || coalesce(public._amose_format_naira(v_qty * v_price), '?');
      end if;
    else
      v_line := v_line || '.';
    end if;
    return 'Please check this sale:' || pg_catalog.chr(10) || pg_catalog.chr(10)
      || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch || v_total;
  elsif p_kind = 'production' or p_kind = 'poultry_daily_report' then
    begin
      v_qty := (v_fields ->> 'good_quantity')::numeric;
    exception when others then
      v_qty := null;
    end;
    if v_qty is not null and v_qty >= 0 then
      v_line := v_reporter || ' produced ' || v_qty::bigint::text || ' bags.';
    else
      v_line := v_reporter || ' sent a production report.';
    end if;
    return 'Please check this production report:'
      || pg_catalog.chr(10) || pg_catalog.chr(10)
      || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  elsif p_kind = 'expense' then
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint / 100.0;
    exception when others then
      v_amount := null;
    end;
    v_method := coalesce(nullif(btrim(v_fields ->> 'category'), ''),
      'spending');
    if v_amount is not null then
      v_line := v_reporter || ' spent '
        || coalesce(public._amose_format_naira(v_amount), '?')
        || ' on ' || pg_catalog.left(v_method, 24) || '.';
    else
      v_line := v_reporter || ' sent an expense report.';
    end if;
    return 'Please check this expense:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  elsif p_kind = 'bank_deposit' then
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint / 100.0;
    exception when others then
      v_amount := null;
    end;
    if v_amount is not null then
      v_line := v_reporter || ' deposited '
        || coalesce(public._amose_format_naira(v_amount), '?') || '.';
    else
      v_line := v_reporter || ' sent a deposit report.';
    end if;
    return 'Please check this deposit:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  elsif p_kind = 'payment' then
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint / 100.0;
    exception when others then
      v_amount := null;
    end;
    v_method := nullif(btrim(v_fields ->> 'method'), '');
    if v_amount is not null then
      v_line := v_reporter || ' collected '
        || coalesce(public._amose_format_naira(v_amount), '?');
      if v_method is not null then
        v_line := v_line || ' (' || pg_catalog.left(v_method, 12) || ')';
      end if;
      v_line := v_line || '.';
    else
      v_line := v_reporter || ' sent a payment report.';
    end if;
    return 'Please check this payment:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  elsif p_kind = 'stock' then
    v_line := v_reporter || ' counted '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'normal_quantity', ''), 12), '?')
      || ' normal and '
      || coalesce(pg_catalog.left(nullif(v_fields ->> 'cold_quantity', ''), 12), '?')
      || ' cold bags.';
    return 'Please check this stock count:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  elsif p_kind = 'customer_payment' then
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint / 100.0;
    exception when others then
      v_amount := null;
    end;
    v_method := coalesce(nullif(btrim(v_fields ->> 'customer_name'), ''), 'A customer');
    if v_amount is not null then
      v_line := pg_catalog.left(v_method, 24) || ' paid '
        || coalesce(public._amose_format_naira(v_amount), '?') || '.';
    else
      v_line := v_reporter || ' sent a customer payment report.';
    end if;
    return 'Please check this customer payment:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  elsif p_kind = 'customer_debt' then
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint / 100.0;
    exception when others then
      v_amount := null;
    end;
    v_method := coalesce(nullif(btrim(v_fields ->> 'customer_name'), ''), 'A customer');
    if v_amount is not null then
      v_line := pg_catalog.left(v_method, 24) || ' owes '
        || coalesce(public._amose_format_naira(v_amount), '?') || '.';
    else
      v_line := v_reporter || ' sent a customer debt report.';
    end if;
    return 'Please check this customer debt:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_line || pg_catalog.chr(10)
      || 'Branch: ' || v_branch;
  else
    return 'Please check this report:' || pg_catalog.chr(10)
      || pg_catalog.chr(10) || v_reporter || ' sent a report.'
      || pg_catalog.chr(10) || 'Branch: ' || v_branch;
  end if;
end;
$func$;

revoke execute on function public._amose_telegram_review_text(uuid, text, text, text, jsonb, text)
  from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 3. Telegram queueing sends the plain-language reviewer text (with the
-- reporter's display name) instead of the technical request text. The
-- body below is the migration-22 function verbatim except the reporter
-- lookup and the v_msg call site. Kind gate, authorization, separation
-- of duties, routing, idempotency, and grants are unchanged. The
-- WhatsApp queue function is untouched.
-- ---------------------------------------------------------------------------
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
  v_reporter text;
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

  -- Reporter display name for the plain-language reviewer text; a
  -- missing name falls back to 'Staff' inside the helper.
  select nullif(btrim(e.display_name), '') into v_reporter
    from public.biz_employees e
    where e.tenant_id = v_tenant and e.id = v_submitter;

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

  v_msg := public._amose_telegram_review_text(
    v_tenant, v_business, v_branch, v_kind, v_parsed, v_reporter);

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

-- CREATE OR REPLACE preserves the service-role-only grant; restated
-- here so the matrix stays explicit.
revoke execute on function public.amose_queue_telegram_review_requests(uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_queue_telegram_review_requests(uuid, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- 4. Plain-language reporter acknowledgements. The bodies below are the
-- current functions verbatim except the queued ack message_text: a short
-- confirmation plus the 4-character user-facing code, no UUIDs, keys, or
-- database terms. Approval/rejection audit, posting, idempotency, routing,
-- and grants are unchanged on both paths.
-- ---------------------------------------------------------------------------
create or replace function public._amose_simple_result_line(
  p_kind text,
  p_verified jsonb
)
returns text
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_ver jsonb;
  v_line jsonb;
  v_qty bigint; v_price_kobo bigint; v_unit text; v_method text;
  v_amount_kobo bigint; v_text text;
begin
  v_ver := case when jsonb_typeof(p_verified) = 'object'
    then p_verified else '{}'::jsonb end;
  if p_kind = 'sale' then
    v_line := case when jsonb_typeof(v_ver -> 'lines') = 'array'
        and jsonb_array_length(v_ver -> 'lines') > 0
      then v_ver -> 'lines' -> 0 else '{}'::jsonb end;
    begin
      v_qty := (v_line ->> 'quantity')::bigint;
      v_price_kobo := (v_line ->> 'unit_price_kobo')::bigint;
    exception when others then
      v_qty := null; v_price_kobo := null;
    end;
    v_unit := nullif(btrim(v_line ->> 'unit'), '');
    if v_unit is null then
      v_unit := coalesce(nullif(btrim(v_ver ->> 'unit'), ''), 'bag');
    end if;
    v_unit := pg_catalog.left(v_unit, 16);
    v_method := nullif(btrim(
      coalesce(v_line ->> 'payment_method', v_ver ->> 'payment_method')), '');
    if v_qty is not null and v_price_kobo is not null
        and v_qty >= 0 and v_price_kobo >= 0 then
      if v_qty = 1 and pg_catalog.right(v_unit, 1) = 's' then
        v_unit := pg_catalog.left(v_unit,
          pg_catalog.char_length(v_unit) - 1);
      elsif v_qty <> 1 and pg_catalog.right(v_unit, 1) <> 's' then
        v_unit := v_unit || 's';
      end if;
      v_text := v_qty::text || ' ' || v_unit || ' sold for '
        || coalesce(
          public._amose_format_naira(v_price_kobo / 100.0), '?');
      if v_method is not null then
        v_text := v_text || ' ' || pg_catalog.left(v_method, 12);
      end if;
      return v_text || '.';
    end if;
    return 'Sale recorded.';
  elsif p_kind = 'production' or p_kind = 'poultry_daily_report' then
    begin
      v_qty := (v_ver ->> 'good_quantity')::bigint;
    exception when others then
      v_qty := null;
    end;
    if v_qty is not null and v_qty >= 0 then
      return v_qty::text || ' bags produced.';
    end if;
    return 'Production recorded.';
  elsif p_kind = 'expense' then
    begin
      v_amount_kobo := (v_ver ->> 'amount_kobo')::bigint;
    exception when others then
      v_amount_kobo := null;
    end;
    v_text := coalesce(nullif(btrim(v_ver ->> 'category'), ''), 'spending');
    if v_amount_kobo is not null and v_amount_kobo >= 0 then
      return coalesce(
          public._amose_format_naira(v_amount_kobo / 100.0), '?')
        || ' spent on ' || pg_catalog.left(v_text, 24) || '.';
    end if;
    return 'Expense recorded.';
  elsif p_kind = 'bank_deposit' then
    begin
      v_amount_kobo := (v_ver ->> 'amount_kobo')::bigint;
    exception when others then
      v_amount_kobo := null;
    end;
    if v_amount_kobo is not null and v_amount_kobo >= 0 then
      return coalesce(
          public._amose_format_naira(v_amount_kobo / 100.0), '?')
        || ' deposited.';
    end if;
    return 'Deposit recorded.';
  elsif p_kind = 'payment' then
    begin
      v_amount_kobo := (v_ver ->> 'amount_kobo')::bigint;
    exception when others then
      v_amount_kobo := null;
    end;
    v_method := nullif(btrim(v_ver ->> 'method'), '');
    if v_amount_kobo is not null and v_amount_kobo >= 0 then
      v_text := coalesce(
        public._amose_format_naira(v_amount_kobo / 100.0), '?')
        || ' collected';
      if v_method is not null then
        v_text := v_text || ' (' || pg_catalog.left(v_method, 12) || ')';
      end if;
      return v_text || '.';
    end if;
    return 'Payment recorded.';
  elsif p_kind = 'cash_handover' then
    begin
      v_amount_kobo := (v_ver ->> 'amount_kobo')::bigint;
    exception when others then
      v_amount_kobo := null;
    end;
    if v_amount_kobo is not null and v_amount_kobo >= 0 then
      return coalesce(
          public._amose_format_naira(v_amount_kobo / 100.0), '?')
        || ' handed over.';
    end if;
    return 'Handover recorded.';
  elsif p_kind = 'stock' then
    return coalesce(pg_catalog.left(nullif(v_ver ->> 'normal_quantity', ''), 12), '?')
      || ' normal and '
      || coalesce(pg_catalog.left(nullif(v_ver ->> 'cold_quantity', ''), 12), '?')
      || ' cold bags counted.';
  elsif p_kind = 'customer_payment' then
    begin
      v_amount_kobo := (v_ver ->> 'amount_kobo')::bigint;
    exception when others then
      v_amount_kobo := null;
    end;
    v_text := coalesce(nullif(btrim(v_ver ->> 'customer_name'), ''),
      'Customer payment');
    if v_amount_kobo is not null and v_amount_kobo >= 0 then
      return pg_catalog.left(v_text, 24) || ' paid '
        || coalesce(
          public._amose_format_naira(v_amount_kobo / 100.0), '?') || '.';
    end if;
    return 'Customer payment recorded.';
  elsif p_kind = 'customer_debt' then
    begin
      v_amount_kobo := (v_ver ->> 'amount_kobo')::bigint;
    exception when others then
      v_amount_kobo := null;
    end;
    v_text := coalesce(nullif(btrim(v_ver ->> 'customer_name'), ''),
      'Customer debt');
    if v_amount_kobo is not null and v_amount_kobo >= 0 then
      return pg_catalog.left(v_text, 24) || ' owes '
        || coalesce(
          public._amose_format_naira(v_amount_kobo / 100.0), '?') || '.';
    end if;
    return 'Customer debt recorded.';
  else
    return 'Report recorded.';
  end if;
end;
$func$;

revoke execute on function public._amose_simple_result_line(text, jsonb)
  from public, anon, authenticated, service_role;

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
  v_code text; v_result_line text;
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
  -- sent here): a short confirmation plus the user-facing code. No
  -- UUIDs, keys, or database terms. The destination is the original
  -- report sender and the provider account is the original inbound
  -- snapshot -- both resolved authoritatively above. The idempotency
  -- key is tenant-scoped so tenants never collide.
  v_code := coalesce(public._amose_short_code(v_ref), '?');
  v_result_line := public._amose_simple_result_line(v_kind, p_verified);
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id,
     provider, provider_sender, provider_account,
     related_task_id, message_type, message_text, status, idempotency_key)
    values (v_tenant, v_business, v_branch, v_submitter,
      v_ack_provider, v_ack_sender, v_acct,
      null, 'review_confirmed',
      'Approved ' || pg_catalog.chr(10) || v_result_line
        || pg_catalog.chr(10) || 'Code: ' || v_code,
      'queued', 'review_ack:' || v_tenant::text || ':' || v_key);

  return v_out;
end;
$func$;

-- CREATE OR REPLACE preserves the service-role-only grant; restated
-- here so the matrix stays explicit.
revoke execute on function public.amose_confirm_submission(
  text, text, text, jsonb, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_confirm_submission(text, text, text,
  jsonb, text, text)
  to service_role;

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
  v_code text;
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

  -- Short refusal plus the user-facing code through the durable queue
  -- only. No UUIDs, keys, or database terms.
  v_code := coalesce(public._amose_short_code(v_ref), '?');
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id,
     provider, provider_sender, provider_account,
     related_task_id, message_type, message_text, status, idempotency_key)
    values (v_tenant, v_business, v_branch, v_submitter,
      v_ack_provider, v_ack_sender, v_acct,
      null, 'review_rejected',
      'Not approved.' || pg_catalog.chr(10)
        || 'Reason: ' || pg_catalog.left(v_reason, 200)
        || pg_catalog.chr(10) || 'Code: ' || v_code,
      'queued', 'review_ack:' || v_tenant::text || ':' || v_key);

  return v_out;
end;
$func$;

-- CREATE OR REPLACE preserves the service-role-only grant; restated
-- here so the matrix stays explicit.
revoke execute on function public.amose_reject_submission(
  text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_reject_submission(
  text, text, text, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- 5. Guided Telegram decision flows. Opaque button tokens plus staged
-- values/reasons live here; the review itself still runs only in the
-- Phase 3/4 RPCs. Every press re-checks linked identity, reviewer
-- authorization, scope, self-review prevention, expiry, and single-use.
-- ---------------------------------------------------------------------------
create table public.biz_telegram_flow_tokens (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  reviewer_employee_id uuid not null,
  business_id text,
  branch_id text,
  submission_id uuid,
  review_ref text,
  flow text not null check (flow in ('correct_menu', 'correct_field',
    'correct_save', 'correct_back', 'correct_cancel', 'correct_cancel_yes',
    'reject_menu', 'report_branch')),
  field text,
  token_hash text not null unique,
  request_key text not null,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, reviewer_employee_id)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  check (token_hash ~ '^[0-9a-f]{64}$'),
  check (pg_catalog.char_length(request_key) between 1 and 128),
  check (field is null or pg_catalog.char_length(field) between 1 and 64),
  check (business_id is null or btrim(business_id) <> ''),
  check (branch_id is null or btrim(branch_id) <> ''),
  check (expires_at > created_at),
  check (used_at is null or used_at >= created_at),
  check ((review_ref is null) = (submission_id is null)),
  check ((flow = 'report_branch') = (review_ref is null)),
  unique (tenant_id, request_key)
);
comment on table public.biz_telegram_flow_tokens is
  'Opaque Telegram guided-flow button tokens (sha256 hex only). Branch-choice buttons carry no case; decision-flow buttons bind one open case and reviewer. Consumed by amose_consume_telegram_flow_token.';
create index biz_telegram_flow_tokens_case
  on public.biz_telegram_flow_tokens(tenant_id, review_ref)
  where review_ref is not null;

create table public.biz_telegram_flow_state (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text,
  branch_id text,
  submission_id uuid,
  review_ref text,
  reviewer_employee_id uuid not null,
  flow text not null check (flow in ('report_branch', 'correct',
    'reject')),
  field text,
  value_text text,
  request_key text not null,
  expires_at timestamptz not null,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, reviewer_employee_id)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  check (pg_catalog.char_length(request_key) between 1 and 128),
  check (field is null or pg_catalog.char_length(field) between 1 and 64),
  check (value_text is null or pg_catalog.char_length(value_text) between 1 and 200),
  check ((review_ref is null) = (submission_id is null)),
  check ((flow = 'report_branch') = (review_ref is null)),
  check (expires_at > created_at)
);
comment on table public.biz_telegram_flow_state is
  'Staged guided-flow input: one pending branch choice, correction, or rejection reason per reviewer (and case). Written only by amose_stage_telegram_flow; values are applied only through the review RPCs.';

alter table public.biz_telegram_flow_tokens enable row level security;
alter table public.biz_telegram_flow_state enable row level security;
revoke all on public.biz_telegram_flow_tokens,
  public.biz_telegram_flow_state
  from public, anon, authenticated, service_role;

-- Mint one guided-flow button token. Case flows bind an open case and an
-- authorized non-submitter reviewer; branch flows bind a linked sender
-- with a candidate scope and no case. Superseded tokens for the same
-- (tenant, flow, reviewer, case-or-null) are retired first.
create or replace function public.amose_mint_telegram_flow_token(
  p_token_hash text,
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_review_ref text,
  p_flow text,
  p_field text,
  p_business text,
  p_branch text,
  p_request_key text,
  p_expires_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_hash text;
  v_sender text;
  v_key text;
  v_field text;
  v_business text;
  v_branch text;
  v_tenant uuid; v_reviewer uuid;
  v_case_business text; v_case_branch text; v_case_sub uuid;
  v_submitter uuid; v_case_status text; v_need text;
begin
  v_hash := nullif(btrim(p_token_hash), '');
  if v_hash is null or v_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'MALFORMED: token hash must be sha256 hex';
  end if;
  if p_reviewer_provider is null or p_reviewer_provider <> 'telegram' then
    raise exception 'MALFORMED: flow provider must be telegram';
  end if;
  v_sender := nullif(btrim(p_reviewer_sender), '');
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;
  if p_flow is null or p_flow not in ('correct_menu', 'correct_field',
      'correct_save', 'correct_back', 'correct_cancel',
      'correct_cancel_yes', 'reject_menu', 'report_branch') then
    raise exception 'MALFORMED: flow step is not known';
  end if;
  v_field := nullif(btrim(p_field), '');
  if v_field is not null and pg_catalog.char_length(v_field) > 64 then
    raise exception 'MALFORMED: flow field is too long';
  end if;
  v_business := nullif(btrim(p_business), '');
  v_branch := nullif(btrim(p_branch), '');
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  if p_expires_at is null or p_expires_at <= pg_catalog.now()
      or p_expires_at > pg_catalog.now() + interval '30 days' then
    raise exception 'MALFORMED: flow expiry must be within the next 30 days';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, v_sender) as o;
  if v_tenant is null or v_reviewer is null then
    raise exception 'NOT_FOUND: reviewer is not known';
  end if;

  if nullif(btrim(p_review_ref), '') is null then
    -- Branch-choice button: a linked sender plus one candidate scope,
    -- and nothing else. Scope is verified against assignments at
    -- intake time, never trusted from the button.
    if p_flow <> 'report_branch' then
      raise exception 'MALFORMED: caseless flow must be report_branch';
    end if;
    if v_business is null or v_branch is null then
      raise exception 'MALFORMED: branch button needs its scope';
    end if;
    delete from public.biz_telegram_flow_tokens t
      where t.tenant_id = v_tenant
        and t.flow = p_flow
        and t.reviewer_employee_id = v_reviewer
        and t.review_ref is null;
    insert into public.biz_telegram_flow_tokens
      (tenant_id, reviewer_employee_id, business_id, branch_id,
       review_ref, submission_id, flow, field,
       token_hash, request_key, expires_at)
      values (v_tenant, v_reviewer, v_business, v_branch,
        null, null, p_flow, v_field,
        v_hash, v_key, p_expires_at);
    return jsonb_build_object('status', 'minted',
      'tenant_id', v_tenant::text,
      'business_id', v_business, 'branch_id', v_branch,
      'flow', p_flow, 'field', v_field,
      'reviewer_employee_id', v_reviewer::text,
      'request_key', v_key);
  end if;

  if p_flow = 'report_branch' then
    raise exception 'MALFORMED: branch flow carries no case';
  end if;
  if v_business is not null or v_branch is not null then
    raise exception 'MALFORMED: case flow carries no scope';
  end if;
  if p_review_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  select c.business_id, c.branch_id, c.submission_id, s.employee_id,
      c.status
    into v_case_business, v_case_branch, v_case_sub, v_submitter,
      v_case_status
    from public.biz_review_cases c
    join public.biz_submissions s
      on s.tenant_id = c.tenant_id
      and s.business_id = c.business_id
      and s.branch_id = c.branch_id
      and s.id = c.submission_id
    where c.tenant_id = v_tenant
      and c.review_ref = p_review_ref
    for update of c;
  if v_case_business is null then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  if v_case_status is distinct from 'open' then
    raise exception 'CONFLICT: review case is already decided';
  end if;
  if v_reviewer = v_submitter then
    raise exception 'UNAUTHORIZED: the reporter cannot decide their own report';
  end if;
  v_need := case when p_flow = 'reject_menu' then 'reject' else 'confirm' end;
  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_case_business, v_case_branch, v_submitter,
    v_need);

  delete from public.biz_telegram_flow_tokens t
    where t.tenant_id = v_tenant
      and t.flow = p_flow
      and t.reviewer_employee_id = v_reviewer
      and t.review_ref = p_review_ref;
  begin
    insert into public.biz_telegram_flow_tokens
      (tenant_id, reviewer_employee_id, business_id, branch_id,
       submission_id, review_ref, flow, field,
       token_hash, request_key, expires_at)
      values (v_tenant, v_reviewer, v_case_business, v_case_branch,
        v_case_sub, p_review_ref, p_flow, v_field,
        v_hash, v_key, p_expires_at);
  exception when unique_violation then
    raise exception 'CONFLICT: flow token could not be minted';
  end;

  return jsonb_build_object('status', 'minted',
    'tenant_id', v_tenant::text,
    'business_id', v_case_business, 'branch_id', v_case_branch,
    'submission_id', v_case_sub::text,
    'review_ref', p_review_ref,
    'flow', p_flow, 'field', v_field,
    'reviewer_employee_id', v_reviewer::text,
    'request_key', v_key);
end;
$func$;

revoke execute on function public.amose_mint_telegram_flow_token(text, text, text, text, text, text, text, text, text, timestamptz)
  from public, anon, authenticated;
grant execute on function public.amose_mint_telegram_flow_token(text, text, text, text, text, text, text, text, text, timestamptz)
  to service_role;

-- Consume one guided-flow button token. First press returns 'ready'; a
-- repeat returns the SAME stored parameters with 'already_used' so the
-- caller retries the same step instead of advancing twice. Decided or
-- expired presses retire the token and report state with no effect.
-- Unknown tokens read as NOT_FOUND and binding mismatches as
-- UNAUTHORIZED, so forged presses reveal nothing and change nothing.
create or replace function public.amose_consume_telegram_flow_token(
  p_token_hash text,
  p_reviewer_provider text,
  p_reviewer_sender text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_hash text;
  v_sender text;
  v_tok public.biz_telegram_flow_tokens%rowtype;
  v_tenant uuid; v_reviewer uuid;
  v_case_status text;
  v_kind text;
  v_is_retry boolean;
begin
  v_hash := nullif(btrim(p_token_hash), '');
  if v_hash is null or v_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'NOT_FOUND: flow button is not known';
  end if;
  if p_reviewer_provider is null or p_reviewer_provider <> 'telegram' then
    raise exception 'MALFORMED: flow provider must be telegram';
  end if;
  v_sender := nullif(btrim(p_reviewer_sender), '');
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;

  select * into v_tok from public.biz_telegram_flow_tokens t
    where t.token_hash = v_hash
    for update;
  if not found then
    raise exception 'NOT_FOUND: flow button is not known';
  end if;

  -- The presser must be the bound reviewer in the token's own tenant.
  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, v_sender) as o;
  if v_tenant is distinct from v_tok.tenant_id
      or v_reviewer is distinct from v_tok.reviewer_employee_id then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;

  if v_tok.review_ref is not null then
    select c.status into v_case_status
      from public.biz_review_cases c
      where c.tenant_id = v_tok.tenant_id
        and c.review_ref = v_tok.review_ref;
    if not found or v_case_status is distinct from 'open' then
      if v_tok.used_at is null then
        update public.biz_telegram_flow_tokens t
          set used_at = pg_catalog.now()
          where t.id = v_tok.id;
      end if;
      return jsonb_build_object('status', 'case_closed',
        'flow', v_tok.flow, 'field', v_tok.field,
        'review_ref', v_tok.review_ref,
        'request_key', v_tok.request_key,
        'tenant_id', v_tok.tenant_id::text,
        'is_retry', (v_tok.used_at is not null));
    end if;
    select s.kind into v_kind
      from public.biz_submissions s
      where s.tenant_id = v_tok.tenant_id
        and s.business_id = v_tok.business_id
        and s.branch_id = v_tok.branch_id
        and s.id = v_tok.submission_id;
  end if;

  if v_tok.expires_at <= pg_catalog.now() then
    if v_tok.used_at is null then
      update public.biz_telegram_flow_tokens t
        set used_at = pg_catalog.now()
        where t.id = v_tok.id;
    end if;
    return jsonb_build_object('status', 'expired',
      'flow', v_tok.flow, 'field', v_tok.field,
      'review_ref', v_tok.review_ref,
      'request_key', v_tok.request_key,
      'tenant_id', v_tok.tenant_id::text,
      'is_retry', (v_tok.used_at is not null));
  end if;

  v_is_retry := (v_tok.used_at is not null);
  if not v_is_retry then
    update public.biz_telegram_flow_tokens t
      set used_at = pg_catalog.now()
      where t.id = v_tok.id;
  end if;

  if v_is_retry then
    return jsonb_build_object('status', 'already_used',
      'flow', v_tok.flow, 'field', v_tok.field,
      'business_id', v_tok.business_id, 'branch_id', v_tok.branch_id,
      'submission_id', v_tok.submission_id::text,
      'review_ref', v_tok.review_ref,
      'submission_kind', v_kind,
      'request_key', v_tok.request_key,
      'tenant_id', v_tok.tenant_id::text,
      'reviewer_employee_id', v_tok.reviewer_employee_id::text,
      'is_retry', true);
  end if;
  return jsonb_build_object('status', 'ready',
    'flow', v_tok.flow, 'field', v_tok.field,
    'business_id', v_tok.business_id, 'branch_id', v_tok.branch_id,
    'submission_id', v_tok.submission_id::text,
    'review_ref', v_tok.review_ref,
    'submission_kind', v_kind,
    'request_key', v_tok.request_key,
    'tenant_id', v_tok.tenant_id::text,
    'reviewer_employee_id', v_tok.reviewer_employee_id::text,
    'is_retry', false);
end;
$func$;

revoke execute on function public.amose_consume_telegram_flow_token(text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_consume_telegram_flow_token(text, text, text)
  to service_role;

-- Stage (or read, take, or clear) one pending guided-flow input: a
-- correction field+value, a rejection reason, or a branch choice. Case
-- flows re-check the open case, reviewer authorization, and the
-- reporter exclusion on every call; the staged text is applied only
-- through the review RPCs, never here. Returns the staged row plus the
-- current stored value (and unit) for previews.
create or replace function public.amose_stage_telegram_flow(
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_review_ref text,
  p_flow text,
  p_field text,
  p_value_text text,
  p_clear boolean default false,
  p_take boolean default false,
  p_request_key text default null,
  p_business text default null,
  p_branch text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_sender text;
  v_key text;
  v_flow text;
  v_field text;
  v_value text;
  v_scope_business text;
  v_scope_branch text;
  v_tenant uuid; v_reviewer uuid;
  v_case_business text; v_case_branch text; v_case_sub uuid;
  v_submitter uuid; v_case_status text; v_kind text;
  v_payload jsonb;
  v_row public.biz_telegram_flow_state%rowtype;
  v_old text;
  v_unit text;
  v_expires timestamptz;
begin
  v_sender := nullif(btrim(p_reviewer_sender), '');
  if p_reviewer_provider is null or p_reviewer_provider <> 'telegram' then
    raise exception 'MALFORMED: flow provider must be telegram';
  end if;
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_flow := nullif(btrim(p_flow), '');
  if v_flow is not null and v_flow not in ('report_branch', 'correct',
      'reject') then
    raise exception 'MALFORMED: flow is not known';
  end if;
  v_field := nullif(btrim(p_field), '');
  if v_field is not null and pg_catalog.char_length(v_field) > 64 then
    raise exception 'MALFORMED: flow field is too long';
  end if;
  v_value := nullif(btrim(p_value_text), '');
  if v_value is not null and pg_catalog.char_length(v_value) > 200 then
    raise exception 'MALFORMED: flow value is too long';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, v_sender) as o;
  if v_tenant is null or v_reviewer is null then
    raise exception 'NOT_FOUND: sender is not known';
  end if;

  -- Branch-choice flows carry no case: the scope was button-bound at
  -- mint time and is verified against assignments at intake time.
  v_scope_business := nullif(btrim(p_business), '');
  v_scope_branch := nullif(btrim(p_branch), '');
  if nullif(btrim(p_review_ref), '') is null then
    if v_flow is not null and v_flow <> 'report_branch' then
      raise exception 'MALFORMED: caseless flow must be report_branch';
    end if;
    if p_take then
      select * into v_row from public.biz_telegram_flow_state s
        where s.tenant_id = v_tenant
          and s.reviewer_employee_id = v_reviewer
          and s.review_ref is null
          and s.flow = 'report_branch'
          and s.expires_at > pg_catalog.now()
        order by s.created_at desc
        limit 1;
      if not found then
        raise exception 'NOT_FOUND: no pending branch choice';
      end if;
      delete from public.biz_telegram_flow_state s
        where s.id = v_row.id;
      return jsonb_build_object('status', 'taken',
        'flow', v_row.flow, 'field', v_row.field,
        'business_id', v_row.business_id, 'branch_id', v_row.branch_id,
        'tenant_id', v_tenant::text,
        'reviewer_employee_id', v_reviewer::text);
    end if;
    if v_flow is null and v_field is null and v_value is null
        and not coalesce(p_clear, false) then
      select * into v_row from public.biz_telegram_flow_state s
        where s.tenant_id = v_tenant
          and s.reviewer_employee_id = v_reviewer
          and s.review_ref is null
          and s.flow = 'report_branch'
          and s.expires_at > pg_catalog.now()
        order by s.created_at desc
        limit 1;
      if not found then
        raise exception 'NOT_FOUND: no pending branch choice';
      end if;
      return jsonb_build_object('status', 'staged',
        'flow', v_row.flow, 'field', v_row.field,
        'business_id', v_row.business_id, 'branch_id', v_row.branch_id,
        'tenant_id', v_tenant::text,
        'reviewer_employee_id', v_reviewer::text);
    end if;
    if coalesce(p_clear, false) then
      delete from public.biz_telegram_flow_state s
        where s.tenant_id = v_tenant
          and s.reviewer_employee_id = v_reviewer
          and s.review_ref is null;
      return jsonb_build_object('status', 'cleared',
        'tenant_id', v_tenant::text,
        'reviewer_employee_id', v_reviewer::text);
    end if;
    if v_flow is null or v_field is null then
      raise exception 'MALFORMED: branch choice needs its branch';
    end if;
    if v_scope_business is null or v_scope_branch is null then
      raise exception 'MALFORMED: branch choice needs its scope';
    end if;
    delete from public.biz_telegram_flow_state s
      where s.tenant_id = v_tenant
        and s.reviewer_employee_id = v_reviewer
        and s.review_ref is null;
    v_expires := pg_catalog.now() + interval '3 days';
    insert into public.biz_telegram_flow_state
      (tenant_id, business_id, branch_id, submission_id, review_ref,
       reviewer_employee_id, flow, field, value_text, request_key,
       expires_at)
      values (v_tenant, v_scope_business, v_scope_branch, null, null,
        v_reviewer, 'report_branch', v_field, null, v_key, v_expires)
      returning * into v_row;
    return jsonb_build_object('status', 'staged',
      'flow', v_row.flow, 'field', v_row.field,
      'business_id', v_row.business_id, 'branch_id', v_row.branch_id,
      'tenant_id', v_tenant::text,
      'reviewer_employee_id', v_reviewer::text);
  end if;

  if p_review_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  if v_flow is not null and v_flow = 'report_branch' then
    raise exception 'MALFORMED: branch flow carries no case';
  end if;
  if v_scope_business is not null or v_scope_branch is not null then
    raise exception 'MALFORMED: case flow carries no scope';
  end if;

  select c.business_id, c.branch_id, c.submission_id, s.employee_id,
      c.status, s.kind, s.payload
    into v_case_business, v_case_branch, v_case_sub, v_submitter,
      v_case_status, v_kind, v_payload
    from public.biz_review_cases c
    join public.biz_submissions s
      on s.tenant_id = c.tenant_id
      and s.business_id = c.business_id
      and s.branch_id = c.branch_id
      and s.id = c.submission_id
    where c.tenant_id = v_tenant
      and c.review_ref = p_review_ref
    for update of c;
  if v_case_business is null then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  if v_case_status is distinct from 'open' then
    raise exception 'CONFLICT: review case is already decided';
  end if;
  if v_reviewer = v_submitter then
    raise exception 'UNAUTHORIZED: the reporter cannot decide their own report';
  end if;

  -- Read mode: return the pending step, if any.
  if v_flow is null and v_field is null and v_value is null
      and not coalesce(p_clear, false) and not coalesce(p_take, false) then
    select * into v_row from public.biz_telegram_flow_state s
      where s.tenant_id = v_tenant
        and s.reviewer_employee_id = v_reviewer
        and s.review_ref = p_review_ref
        and s.expires_at > pg_catalog.now()
      order by s.created_at desc
      limit 1;
    if not found then
      raise exception 'NOT_FOUND: no pending guided step';
    end if;
    v_old := case
      when jsonb_typeof(v_payload -> 'parsed') = 'object'
        and jsonb_typeof(v_payload -> 'parsed' -> 'fields') = 'object'
      then v_payload -> 'parsed' -> 'fields' ->> v_row.field
      else null end;
    v_unit := case
      when jsonb_typeof(v_payload -> 'parsed') = 'object'
        and jsonb_typeof(v_payload -> 'parsed' -> 'fields') = 'object'
      then v_payload -> 'parsed' -> 'fields' ->> 'unit'
      else null end;
    return jsonb_build_object('status', 'staged',
      'flow', v_row.flow, 'field', v_row.field,
      'value_text', v_row.value_text,
      'old_value', v_old, 'unit', v_unit,
      'business_id', v_row.business_id, 'branch_id', v_row.branch_id,
      'submission_id', v_row.submission_id::text,
      'review_ref', v_row.review_ref,
      'submission_kind', v_kind,
      'tenant_id', v_tenant::text,
      'reviewer_employee_id', v_reviewer::text);
  end if;

  if coalesce(p_take, false) then
    raise exception 'MALFORMED: case flows are staged, not taken';
  end if;
  if coalesce(p_clear, false) then
    delete from public.biz_telegram_flow_state s
      where s.tenant_id = v_tenant
        and s.reviewer_employee_id = v_reviewer
        and s.review_ref = p_review_ref;
    return jsonb_build_object('status', 'cleared',
      'review_ref', p_review_ref,
      'tenant_id', v_tenant::text,
      'reviewer_employee_id', v_reviewer::text);
  end if;

  -- Stage mode: determine the effective flow, authorize it, then store.
  select * into v_row from public.biz_telegram_flow_state s
    where s.tenant_id = v_tenant
      and s.reviewer_employee_id = v_reviewer
      and s.review_ref = p_review_ref
      and s.expires_at > pg_catalog.now()
    order by s.created_at desc
    limit 1;
  if v_flow is null then
    if v_row.flow is null then
      raise exception 'NOT_FOUND: no pending guided step';
    end if;
    v_flow := v_row.flow;
  end if;
  if v_flow not in ('correct', 'reject') then
    raise exception 'MALFORMED: flow is not known';
  end if;
  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_case_business, v_case_branch, v_submitter,
    case when v_flow = 'reject' then 'reject' else 'confirm' end);

  if v_field is null then
    v_field := v_row.field;
  end if;
  if v_value is null then
    v_value := v_row.value_text;
  end if;
  delete from public.biz_telegram_flow_state s
    where s.tenant_id = v_tenant
      and s.reviewer_employee_id = v_reviewer
      and s.review_ref = p_review_ref;
  v_expires := pg_catalog.now() + interval '3 days';
  insert into public.biz_telegram_flow_state
    (tenant_id, business_id, branch_id, submission_id, review_ref,
     reviewer_employee_id, flow, field, value_text, request_key,
     expires_at)
    values (v_tenant, v_case_business, v_case_branch, v_case_sub,
      p_review_ref, v_reviewer, v_flow, v_field, v_value, v_key,
      v_expires)
    returning * into v_row;
  v_old := case
    when jsonb_typeof(v_payload -> 'parsed') = 'object'
      and jsonb_typeof(v_payload -> 'parsed' -> 'fields') = 'object'
      and v_field is not null
    then v_payload -> 'parsed' -> 'fields' ->> v_field
    else null end;
  v_unit := case
    when jsonb_typeof(v_payload -> 'parsed') = 'object'
      and jsonb_typeof(v_payload -> 'parsed' -> 'fields') = 'object'
    then v_payload -> 'parsed' -> 'fields' ->> 'unit'
    else null end;
  return jsonb_build_object('status', 'staged',
    'flow', v_row.flow, 'field', v_row.field,
    'value_text', v_row.value_text,
    'old_value', v_old, 'unit', v_unit,
    'business_id', v_row.business_id, 'branch_id', v_row.branch_id,
    'submission_id', v_row.submission_id::text,
    'review_ref', v_row.review_ref,
    'submission_kind', v_kind,
    'tenant_id', v_tenant::text,
    'reviewer_employee_id', v_reviewer::text);
end;
$func$;

revoke execute on function public.amose_stage_telegram_flow(text, text, text, text, text, text, boolean, boolean, text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_stage_telegram_flow(text, text, text, text, text, text, boolean, boolean, text, text, text)
  to service_role;

-- Lists the caller's own live pending flows (branch choices and staged
-- decision inputs) so an ordinary text message can answer the pending
-- prompt it follows. Unknown senders read as empty, never as an error;
-- a sender only ever sees their own rows.
create or replace function public.amose_read_telegram_flows(
  p_reviewer_provider text,
  p_reviewer_sender text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_sender text;
  v_tenant uuid; v_reviewer uuid;
  v_flows jsonb := '[]'::jsonb;
  r record;
begin
  v_sender := nullif(btrim(p_reviewer_sender), '');
  if p_reviewer_provider is null or p_reviewer_provider <> 'telegram' then
    raise exception 'MALFORMED: flow provider must be telegram';
  end if;
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, v_sender) as o;
  if v_tenant is null or v_reviewer is null then
    return jsonb_build_object('status', 'ok', 'flows', v_flows);
  end if;

  for r in
    select s.review_ref, s.flow, s.field, s.value_text,
        s.business_id, s.branch_id, s.expires_at, sub.kind
      from public.biz_telegram_flow_state s
      left join public.biz_submissions sub
        on sub.tenant_id = s.tenant_id
        and sub.business_id = s.business_id
        and sub.branch_id = s.branch_id
        and sub.id = s.submission_id
      where s.tenant_id = v_tenant
        and s.reviewer_employee_id = v_reviewer
        and s.expires_at > pg_catalog.now()
      order by s.created_at desc
  loop
    v_flows := v_flows || jsonb_build_object(
      'review_ref', r.review_ref, 'flow', r.flow, 'field', r.field,
      'value_text', r.value_text, 'business_id', r.business_id,
      'branch_id', r.branch_id, 'submission_kind', r.kind,
      'expires_at', r.expires_at);
  end loop;
  return jsonb_build_object('status', 'ok', 'flows', v_flows);
end;
$func$;

revoke execute on function public.amose_read_telegram_flows(text, text)
  from public, anon, authenticated;
grant execute on function public.amose_read_telegram_flows(text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- 6. Audited reversal of confirmed transactions. 'reversed' joins the
-- submission, case, and audit state machines. One reversal row per
-- original submission makes double reversal impossible; request keys
-- make operator retries idempotent. Every operational posting created
-- by the original confirmation is voided/reversed with the tables'
-- native audited terminal states (plus offsetting append-only rows
-- where ledgers are immutable); original evidence is never updated or
-- deleted. A corrected replacement always flows through a separately
-- reviewed new submission -- this RPC never invents one.
-- ---------------------------------------------------------------------------
alter table public.biz_submissions
  drop constraint if exists biz_submissions_status_check;
alter table public.biz_submissions
  add constraint biz_submissions_status_check
  check (status in ('draft', 'confirmed', 'rejected', 'cancelled',
    'reversed'));

alter table public.biz_review_cases
  drop constraint if exists biz_review_cases_status_check;
alter table public.biz_review_cases
  add constraint biz_review_cases_status_check
  check (status in ('open', 'confirmed', 'rejected', 'cancelled',
    'reversed'));

alter table public.biz_review_cases
  drop constraint if exists biz_review_cases_check;
alter table public.biz_review_cases
  add constraint biz_review_cases_check
  check (((status = 'open' and decided_at is null)
    or (status in ('confirmed', 'rejected', 'cancelled', 'reversed')
      and decided_at is not null)));

alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_action_check;
alter table public.biz_review_audit
  add constraint biz_review_audit_action_check
  check (action in ('corrected', 'confirmed', 'rejected', 'cancelled',
    'reversed'));

alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_check;
alter table public.biz_review_audit
  add constraint biz_review_audit_check
  check (((action = 'rejected'
      and reason is not null and btrim(reason) <> '')
    or (action = 'corrected'
      and reason is not null and btrim(reason) <> '')
    or (action = 'confirmed' and reason is null)
    or (action = 'cancelled'
      and (reason is null or btrim(reason) <> ''))
    or (action = 'reversed'
      and reason is not null and btrim(reason) <> '')));

alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_check1;
alter table public.biz_review_audit
  add constraint biz_review_audit_check1
  check (((action in ('corrected', 'confirmed')
      and verified_snapshot is not null
      and jsonb_typeof(verified_snapshot) = 'object'
      and posting_type is not null and result is not null)
    or (action = 'rejected'
      and verified_snapshot is null
      and posting_type is null and result is not null)
    or (action = 'cancelled'
      and verified_snapshot is null
      and posting_type is null and result is not null)
    or (action = 'reversed'
      and verified_snapshot is null
      and posting_type is not null and result is not null)));

-- The pre-existing case-immutability guard freezes decided cases, which
-- would block the confirmed -> reversed transition this section owns.
-- Narrow one audited exception: a confirmed case may advance to
-- 'reversed' (the reversal RPC owns the reversal row, the voided
-- postings, and the audit row in the same transaction). Deletes, any
-- identity change, and every other reopening stay forbidden exactly as
-- before.
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
  if OLD.tenant_id is distinct from NEW.tenant_id
      or OLD.business_id is distinct from NEW.business_id
      or OLD.branch_id is distinct from NEW.branch_id
      or OLD.submission_id is distinct from NEW.submission_id
      or OLD.review_ref is distinct from NEW.review_ref
      or OLD.request_key is distinct from NEW.request_key then
    raise exception 'INCOMPLETE: review case identity is immutable';
  end if;
  if OLD.status = 'confirmed' or OLD.status = 'rejected'
      or OLD.status = 'cancelled' then
    if NEW.status is distinct from OLD.status
        and not (OLD.status = 'confirmed'
          and NEW.status = 'reversed') then
      raise exception 'NOT_REVIEWABLE: a decided review case cannot be reopened';
    end if;
  end if;
  return NEW;
end;
$func$;

revoke execute on function public._amose_guard_review_cases_immutable()
  from public, anon, authenticated, service_role;

create table public.biz_review_reversals (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  original_submission_id uuid not null,
  review_ref text not null,
  posting_type text not null,
  requested_by uuid not null,
  reason text not null,
  request_key text not null,
  reversed_summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, requested_by)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, original_submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  check (btrim(reason) <> ''),
  -- One reversal per original submission: double reversal is impossible.
  unique (tenant_id, original_submission_id),
  -- Operator retries reuse the request key: identical retries return the
  -- stored result, anything else fails closed.
  unique (tenant_id, request_key)
);
comment on table public.biz_review_reversals is
  'Audited reversals of confirmed submissions. Original evidence is never updated or deleted; operational postings are voided/reversed with native audited states plus offsetting append-only rows. A corrected replacement always arrives as a separately reviewed new submission.';

alter table public.biz_review_reversals enable row level security;
revoke all on public.biz_review_reversals
  from public, anon, authenticated, service_role;
grant select, insert on public.biz_review_reversals to service_role;

-- Reverse one confirmed submission with full audit. The requester must
-- be an authorized reviewer (confirm capability) in the scope and must
-- not be the original reporter. Only 'confirmed' submissions reverse;
-- every operational row posted from this submission is voided or
-- reversed in the same transaction, or the whole call fails.
create or replace function public.amose_reverse_confirmed_submission(
  p_review_ref text,
  p_requester_provider text,
  p_requester_sender text,
  p_reason text,
  p_request_key text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_ref text;
  v_key text;
  v_reason text;
  v_tenant uuid; v_requester uuid;
  v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text;
  v_case_id uuid; v_case_status text;
  v_posting text;
  v_audit_id uuid;
  v_reversal_id uuid;
  v_out jsonb; v_existing jsonb;
  v_orig_sub uuid; v_orig_ref text; v_orig_reason text;
  v_sale record;
  v_move record;
  v_pay record;
  v_cust record;
  v_mem record;
  v_text text;
  v_reversed jsonb := '[]'::jsonb;
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
    raise exception 'MALFORMED: reversal requires a non-blank reason';
  end if;
  if pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: reversal reason is too long';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_requester
    from public._amose_resolve_reviewer_identity(
      p_requester_provider, p_requester_sender) as o;
  if v_tenant is null or v_requester is null then
    raise exception 'NOT_FOUND: requester is not known';
  end if;

  select c.id, c.business_id, c.branch_id, c.submission_id, c.status
    into v_case_id, v_business, v_branch, v_sub, v_case_status
    from public.biz_review_cases c
    where c.tenant_id = v_tenant
      and c.review_ref = v_ref
    for update;
  if v_business is null then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  select s.employee_id, s.kind, s.status
    into v_submitter, v_kind, v_status
    from public.biz_submissions s
    where s.tenant_id = v_tenant
      and s.business_id = v_business
      and s.branch_id = v_branch
      and s.id = v_sub
    for update;
  if v_submitter is null then
    raise exception 'INCOMPLETE: review case % has no submission', v_ref;
  end if;

  -- Requester authorization plus reporter exclusion: an authorized
  -- reviewer who did not file the report. Checked BEFORE the
  -- idempotent retry is returned.
  perform public._amose_authorize_reviewer(
    v_requester, v_tenant, v_business, v_branch, v_submitter, 'confirm');
  if v_requester = v_submitter then
    raise exception 'UNAUTHORIZED: the reporter cannot reverse their own confirmation';
  end if;

  -- Idempotent retry: the same key on the same original reason returns
  -- the stored result; the same key elsewhere, or any second reversal
  -- of this submission, fails closed.
  select r.original_submission_id, r.review_ref, r.reason,
      r.reversed_summary
    into v_orig_sub, v_orig_ref, v_orig_reason, v_existing
    from public.biz_review_reversals r
    where r.tenant_id = v_tenant
      and r.request_key = v_key;
  if found then
    if v_orig_sub is distinct from v_sub
        or v_orig_ref is distinct from v_ref
        or v_orig_reason is distinct from v_reason then
      raise exception 'CONFLICT: request key was already used for a different reversal';
    end if;
    if v_status is distinct from 'reversed' then
      raise exception 'INCOMPLETE: submission % has a reversal record but status %', v_sub, v_status;
    end if;
    return (v_existing || jsonb_build_object('is_retry', true));
  end if;
  if exists (select 1 from public.biz_review_reversals r
      where r.tenant_id = v_tenant
        and r.original_submission_id = v_sub) then
    raise exception 'CONFLICT: submission % was already reversed', v_sub;
  end if;
  -- Fresh reversals only: anything but a confirmed submission (already
  -- reversed above, rejected, or otherwise decided) fails closed here,
  -- after the retry path so identical operator retries still return
  -- the stored result.
  if v_status is distinct from 'confirmed' then
    raise exception 'NOT_REVIEWABLE: submission % has status %, only confirmed submissions can be reversed',
      v_sub, v_status;
  end if;

  -- Posting type comes from the submission's own confirmation audit,
  -- never from caller input.
  select a.posting_type into v_posting
    from public.biz_review_audit a
    where a.tenant_id = v_tenant
      and a.submission_id = v_sub
      and a.action in ('confirmed', 'corrected')
    order by a.created_at desc
    limit 1;
  if v_posting is null then
    raise exception 'INCOMPLETE: submission % has no confirmation audit', v_sub;
  end if;

  -- Reverse every operational posting from this submission, per kind,
  -- using each table's native audited terminal state. Append-only
  -- ledgers get offsetting rows; nothing is updated away or deleted.
  if v_posting = 'sale' then
    for v_sale in
      select s.id from public.biz_sales s
      where s.tenant_id = v_tenant and s.business_id = v_business
        and s.branch_id = v_branch and s.source_submission_id = v_sub
      order by s.id
    loop
      update public.biz_sales s
        set status = 'voided', voided_by = v_requester,
          voided_at = pg_catalog.now(), void_reason = v_reason
        where s.id = v_sale.id
          and s.status in ('confirmed', 'partially_paid', 'paid');
      if not found then
        raise exception 'INCOMPLETE: sale % is not in a voidable state', v_sale.id;
      end if;
      v_reversed := v_reversed || jsonb_build_object(
        'voided_sale_id', v_sale.id::text);
    end loop;
    for v_move in
      select m.id, m.product_id, m.storage_state, m.quantity
      from public.biz_inventory_movements m
      where m.tenant_id = v_tenant and m.business_id = v_business
        and m.branch_id = v_branch and m.source_submission_id = v_sub
        and m.movement_type = 'sale'
      order by m.id
    loop
      insert into public.biz_inventory_movements
        (tenant_id, business_id, branch_id, product_id, movement_type,
         storage_state, quantity, source_submission_id,
         recorded_by, reason, idempotency_key)
        values (v_tenant, v_business, v_branch, v_move.product_id,
          'return', v_move.storage_state, v_move.quantity, v_sub,
          v_requester,
          'reversal of sale movement ' || v_move.id::text || ': ' || v_reason,
          'reversal:' || v_sub::text || ':sale_return:' || v_move.id::text);
      v_reversed := v_reversed || jsonb_build_object(
        'returned_stock_for_movement', v_move.id::text);
    end loop;
    for v_pay in
      select p.id from public.biz_payments p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.branch_id = v_branch and p.source_submission_id = v_sub
      order by p.id
    loop
      update public.biz_payments p
        set status = 'reversed', reversed_by = v_requester,
          reversed_at = pg_catalog.now(), reversal_reason = v_reason
        where p.id = v_pay.id and p.status = 'confirmed';
      if not found then
        raise exception 'INCOMPLETE: payment % is not in a reversible state', v_pay.id;
      end if;
      for v_cust in
        select c.id, c.custodian_id, c.amount_kobo, c.recorded_by
        from public.biz_cash_custody_entries c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.branch_id = v_branch and c.related_payment_id = v_pay.id
        order by c.id
      loop
        insert into public.biz_cash_custody_entries
          (tenant_id, business_id, branch_id, custodian_id, entry_type,
           amount_kobo, related_payment_id, recorded_by, notes,
           idempotency_key)
          values (v_tenant, v_business, v_branch, v_cust.custodian_id,
            'correction', v_cust.amount_kobo, v_pay.id, v_requester,
            'reversal of custody ' || v_cust.id::text || ': ' || v_reason,
            'reversal:' || v_sub::text || ':custody:' || v_cust.id::text);
      end loop;
      v_reversed := v_reversed || jsonb_build_object(
        'reversed_payment_id', v_pay.id::text);
    end loop;
  elsif v_posting = 'payment' then
    for v_pay in
      select p.id from public.biz_payments p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.branch_id = v_branch and p.source_submission_id = v_sub
      order by p.id
    loop
      update public.biz_payments p
        set status = 'reversed', reversed_by = v_requester,
          reversed_at = pg_catalog.now(), reversal_reason = v_reason
        where p.id = v_pay.id and p.status = 'confirmed';
      if not found then
        raise exception 'INCOMPLETE: payment % is not in a reversible state', v_pay.id;
      end if;
      for v_cust in
        select c.id, c.custodian_id, c.amount_kobo, c.recorded_by
        from public.biz_cash_custody_entries c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.branch_id = v_branch and c.related_payment_id = v_pay.id
        order by c.id
      loop
        insert into public.biz_cash_custody_entries
          (tenant_id, business_id, branch_id, custodian_id, entry_type,
           amount_kobo, related_payment_id, recorded_by, notes,
           idempotency_key)
          values (v_tenant, v_business, v_branch, v_cust.custodian_id,
            'correction', v_cust.amount_kobo, v_pay.id, v_requester,
            'reversal of custody ' || v_cust.id::text || ': ' || v_reason,
            'reversal:' || v_sub::text || ':custody:' || v_cust.id::text);
      end loop;
      v_reversed := v_reversed || jsonb_build_object(
        'reversed_payment_id', v_pay.id::text);
    end loop;
    if jsonb_array_length(v_reversed) = 0 then
      raise exception 'INCOMPLETE: submission % posted no reversible payment', v_sub;
    end if;
  elsif v_posting = 'expense' then
    for v_pay in
      select e.id from public.biz_expenses e
      where e.tenant_id = v_tenant and e.business_id = v_business
        and e.branch_id = v_branch and e.source_submission_id = v_sub
      order by e.id
    loop
      update public.biz_expenses e
        set status = 'voided', voided_by = v_requester,
          voided_at = pg_catalog.now(), void_reason = v_reason
        where e.id = v_pay.id and e.status = 'confirmed';
      if not found then
        raise exception 'INCOMPLETE: expense % is not in a voidable state', v_pay.id;
      end if;
      for v_cust in
        select c.id, c.custodian_id, c.amount_kobo, c.recorded_by
        from public.biz_cash_custody_entries c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.branch_id = v_branch and c.related_expense_id = v_pay.id
        order by c.id
      loop
        insert into public.biz_cash_custody_entries
          (tenant_id, business_id, branch_id, custodian_id, entry_type,
           amount_kobo, related_expense_id, recorded_by, notes,
           idempotency_key)
          values (v_tenant, v_business, v_branch, v_cust.custodian_id,
            'correction', v_cust.amount_kobo, v_pay.id, v_requester,
            'reversal of custody ' || v_cust.id::text || ': ' || v_reason,
            'reversal:' || v_sub::text || ':custody:' || v_cust.id::text);
      end loop;
      v_reversed := v_reversed || jsonb_build_object(
        'voided_expense_id', v_pay.id::text);
    end loop;
    if jsonb_array_length(v_reversed) = 0 then
      raise exception 'INCOMPLETE: submission % posted no voidable expense', v_sub;
    end if;
  elsif v_posting = 'production' then
    for v_sale in
      select r.id from public.biz_production_runs r
      where r.tenant_id = v_tenant and r.business_id = v_business
        and r.branch_id = v_branch and r.source_submission_id = v_sub
      order by r.id
    loop
      update public.biz_production_runs r
        set status = 'voided', voided_by = v_requester,
          voided_at = pg_catalog.now(), void_reason = v_reason
        where r.id = v_sale.id and r.status = 'confirmed';
      if not found then
        raise exception 'INCOMPLETE: production run % is not in a voidable state', v_sale.id;
      end if;
      v_reversed := v_reversed || jsonb_build_object(
        'voided_production_run_id', v_sale.id::text);
    end loop;
    if jsonb_array_length(v_reversed) = 0 then
      raise exception 'INCOMPLETE: submission % posted no voidable production run', v_sub;
    end if;
    for v_move in
      select m.id, m.product_id, m.storage_state, m.quantity
      from public.biz_inventory_movements m
      where m.tenant_id = v_tenant and m.business_id = v_business
        and m.branch_id = v_branch and m.source_submission_id = v_sub
        and m.movement_type = 'production'
      order by m.id
    loop
      insert into public.biz_inventory_movements
        (tenant_id, business_id, branch_id, product_id, movement_type,
         storage_state, quantity, source_submission_id,
         recorded_by, reason, idempotency_key)
        values (v_tenant, v_business, v_branch, v_move.product_id,
          'adjustment_out', v_move.storage_state, v_move.quantity, v_sub,
          v_requester,
          'reversal of production movement ' || v_move.id::text || ': ' || v_reason,
          'reversal:' || v_sub::text || ':production_out:' || v_move.id::text);
      v_reversed := v_reversed || jsonb_build_object(
        'offset_production_movement', v_move.id::text);
    end loop;
  elsif v_posting = 'cash_handover' or v_posting = 'bank_deposit' then
    -- Custody postings are append-only with no void state: each original
    -- entry (traced through the posting memory) gets one 'correction'
    -- entry with the same amount and a reversal note. Memories below
    -- are superseded so reports stop counting the original facts.
    for v_mem in
      select mem.content from public.brain_memories mem
      where mem.tenant_id = v_tenant
        and mem.source_table = 'biz_submissions'
        and mem.source_record_id = v_sub::text
    loop
      v_text := nullif(btrim(v_mem.content ->> 'cash_custody_entry_id'), '');
      if v_text is not null then
        select c.id, c.custodian_id, c.amount_kobo, c.recorded_by
          into v_cust
          from public.biz_cash_custody_entries c
          where c.tenant_id = v_tenant and c.business_id = v_business
            and c.branch_id = v_branch
            and c.id = v_text::uuid;
        if found then
          insert into public.biz_cash_custody_entries
            (tenant_id, business_id, branch_id, custodian_id, entry_type,
             amount_kobo, recorded_by, notes, idempotency_key)
            values (v_tenant, v_business, v_branch, v_cust.custodian_id,
              'correction', v_cust.amount_kobo, v_requester,
              'reversal of custody ' || v_cust.id::text || ': ' || v_reason,
              'reversal:' || v_sub::text || ':custody:' || v_cust.id::text);
          v_reversed := v_reversed || jsonb_build_object(
            'corrected_custody_id', v_cust.id::text);
        end if;
      end if;
    end loop;
  elsif v_posting = 'stock' then
    -- Stock counts are append-only point-in-time snapshots with no void
    -- state: reversal records their ids as superseded-by-reversal and
    -- the corrected count arrives as a separately reviewed submission.
    for v_sale in
      select c.id from public.biz_stock_counts c
      where c.tenant_id = v_tenant and c.business_id = v_business
        and c.branch_id = v_branch and c.source_submission_id = v_sub
      order by c.id
    loop
      v_reversed := v_reversed || jsonb_build_object(
        'superseded_stock_count_id', v_sale.id::text);
    end loop;
  elsif v_posting = 'customer_payment' then
    for v_pay in
      select p.id from public.biz_customer_payments p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.branch_id = v_branch and p.source_submission_id = v_sub
      order by p.id
    loop
      update public.biz_customer_payments p
        set status = 'reversed', reversed_by = v_requester,
          reversed_at = pg_catalog.now(), reversal_reason = v_reason
        where p.id = v_pay.id and p.status = 'confirmed';
      if not found then
        raise exception 'INCOMPLETE: customer payment % is not in a reversible state', v_pay.id;
      end if;
      v_reversed := v_reversed || jsonb_build_object(
        'reversed_customer_payment_id', v_pay.id::text);
    end loop;
    if jsonb_array_length(v_reversed) = 0 then
      raise exception 'INCOMPLETE: submission % posted no reversible customer payment', v_sub;
    end if;
  elsif v_posting = 'customer_debt' then
    for v_pay in
      select d.id from public.biz_customer_debts d
      where d.tenant_id = v_tenant and d.business_id = v_business
        and d.branch_id = v_branch and d.source_submission_id = v_sub
      order by d.id
    loop
      update public.biz_customer_debts d
        set status = 'voided', voided_by = v_requester,
          voided_at = pg_catalog.now(), void_reason = v_reason
        where d.id = v_pay.id and d.status = 'open';
      if not found then
        raise exception 'INCOMPLETE: customer debt % is not in a voidable state', v_pay.id;
      end if;
      v_reversed := v_reversed || jsonb_build_object(
        'voided_customer_debt_id', v_pay.id::text);
    end loop;
    if jsonb_array_length(v_reversed) = 0 then
      raise exception 'INCOMPLETE: submission % posted no voidable customer debt', v_sub;
    end if;
  else
    raise exception 'UNSUPPORTED_KIND: posting type % cannot be reversed', v_posting;
  end if;

  -- Posting memories are history: supersede them (never rewrite), so
  -- reports stop treating the original facts as current.
  update public.brain_memories mem
    set verification_status = 'superseded',
      valid_until = pg_catalog.now()
    where mem.tenant_id = v_tenant
      and mem.source_table = 'biz_submissions'
      and mem.source_record_id = v_sub::text
      and mem.verification_status = 'verified';

  update public.biz_submissions s
    set status = 'reversed'
    where s.id = v_sub;

  update public.biz_review_cases c
    set status = 'reversed', decided_at = pg_catalog.now()
    where c.id = v_case_id;

  v_audit_id := pg_catalog.gen_random_uuid();
  v_reversal_id := pg_catalog.gen_random_uuid();
  v_out := jsonb_build_object('status', 'reversed',
    'review_action', 'reversed',
    'submission_kind', v_kind,
    'submission_id', v_sub::text,
    'review_ref', v_ref,
    'posting_type', v_posting,
    'request_key', v_key,
    'reason', v_reason,
    'audit_id', v_audit_id::text,
    'reversal_id', v_reversal_id::text,
    'reversed_postings', v_reversed,
    'is_retry', false);

  insert into public.biz_review_audit
    (id, tenant_id, business_id, branch_id, submission_id, review_ref, action,
     reviewer_employee_id, reason, verified_snapshot, posting_type,
     result, request_key)
    values (v_audit_id, v_tenant, v_business, v_branch, v_sub, v_ref, 'reversed',
      v_requester, v_reason, null, v_posting,
      v_out, v_key);

  insert into public.biz_review_reversals
    (id, tenant_id, business_id, branch_id, original_submission_id,
     review_ref, posting_type, requested_by, reason, request_key,
     reversed_summary)
    values (v_reversal_id, v_tenant, v_business, v_branch, v_sub,
      v_ref, v_posting, v_requester, v_reason, v_key,
      v_out - 'is_retry');

  return v_out;
end;
$func$;

revoke execute on function public.amose_reverse_confirmed_submission(text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_reverse_confirmed_submission(text, text, text, text, text)
  to service_role;

commit;
