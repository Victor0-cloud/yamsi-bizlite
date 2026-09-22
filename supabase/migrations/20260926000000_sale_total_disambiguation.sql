-- Telegram/WhatsApp sale total-vs-unit disambiguation (forward-only).
--
-- Production bug: "SALE 2 bags for 900 naira" stored unit_price = 900
-- (a stated TOTAL silently treated as per-unit), so reviewers saw
-- "sold 2 bags for ₦900" plus "Total: ₦1,800". Parsers now resolve the
-- amount role before any draft exists ("for"/"total" = TOTAL,
-- "at"/"@"/"each" = per-unit, anything else asks the reporter and
-- creates no draft); the confirm path already posts unit_price x100.
--
-- This migration only:
--   1. Admits the new task-less 'sale_clarification' outbound type used
--      by the WhatsApp no-draft clarification question.
--   2. Rewords the two sale display functions so the per-unit figure is
--      always named ("at ₦450 each", "Total: ₦900"); numbers were already
--      fixed by the parser change. All other branches are byte-identical
--      to migration 20260925000000.
-- No earlier migration is edited. No data is touched.

-- ---------------------------------------------------------------------------
-- 1. sale_clarification joins the task-less outbound family.
-- ---------------------------------------------------------------------------
alter table public.biz_outbound_messages
  drop constraint if exists biz_outbound_messages_review_task_check;
alter table public.biz_outbound_messages
  add constraint biz_outbound_messages_review_task_check
  check ((message_type in ('review_confirmed', 'review_rejected',
      'review_request', 'branch_clarification', 'review_cancelled',
      'sale_clarification'))
    = (related_task_id is null));

-- ---------------------------------------------------------------------------
-- 2. Reviewer sale text: per-unit figure always named, total explicit.
-- ---------------------------------------------------------------------------

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
      -- v_price is always per-unit here: parsers resolve a stated total
      -- into unit_price before any draft exists, so a total can never
      -- reach this figure silently. Name it explicitly regardless.
      if v_qty = 1 then
        v_line := v_line || ' for '
          || coalesce(public._amose_format_naira(v_price), '?');
      else
        v_line := v_line || ' at '
          || coalesce(public._amose_format_naira(v_price), '?')
          || ' each';
      end if;
      if v_method is not null then
        v_line := v_line || ' ' || pg_catalog.left(v_method, 12);
      end if;
      v_line := v_line || '.';
      if v_qty is not null and v_qty <> 1 then
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
-- 3. Reporter ack sale line: total named, per-unit parenthetical.
-- ---------------------------------------------------------------------------
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
      -- Name the total whenever more than one unit sold, so a per-unit
      -- figure is never mistaken for the total.
      if v_qty = 1 then
        v_text := v_qty::text || ' ' || v_unit || ' sold for '
          || coalesce(
            public._amose_format_naira(v_price_kobo / 100.0), '?');
      else
        v_text := v_qty::text || ' ' || v_unit || ' sold for '
          || coalesce(public._amose_format_naira(
            v_qty * v_price_kobo / 100.0), '?');
        v_text := v_text || ' ('
          || coalesce(
            public._amose_format_naira(v_price_kobo / 100.0), '?')
          || ' each)';
      end if;
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

commit;
