-- Naira-format regression probes for the review-request text fix.
--
-- NOT a migration: this file is never applied by `supabase db reset`.
-- Run it explicitly against the LOCAL database after a reset:
--
--   supabase db reset --local
--   supabase db execute --local -f supabase/probes/telegram_naira_format_probes.sql
--
-- Background: parsed intake stores NAIRA directly (unit_price 500 means
-- ₦500), but the old formatter divided by 100 ("₦5.00"). Note the JSON
-- contract: callers pass payload->'parsed' (i.e. '{"fields": {...}}') as
-- p_parsed -- the function itself reads p_parsed->'fields'. Every probe is
-- self-validating: any failed assertion raises, aborting the run. A clean
-- run ends with the 'ALL NAIRA FORMAT PROBES PASSED' notice. Pure function
-- calls only -- no fixtures, no writes (rolled back regardless).

begin;

-- N1. Production case: sale unit_price 500 renders ₦500.00 (not ₦5.00).
do $$
declare
  v_text text;
begin
  v_text := public._amose_review_request_text(
    'sale', 'probe-shop', 'main',
    '{"fields": {"quantity": 1, "unit": "bag", "unit_price": 500}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%1 bag at ₦500.00%' then
    raise exception 'N1: sale 500 misrendered: %', v_text;
  end if;
end $$;

-- N2. Larger naira amounts, decimals, and thousands grouping.
do $$
begin
  if public._amose_format_naira(1234567) != '₦1,234,567.00' then
    raise exception 'N2: large amount misrendered: %',
      public._amose_format_naira(1234567);
  end if;
  if public._amose_format_naira(1250.50) != '₦1,250.50' then
    raise exception 'N2: grouped decimal misrendered: %',
      public._amose_format_naira(1250.50);
  end if;
  if public._amose_format_naira(1500.5) != '₦1,500.50' then
    raise exception 'N2: decimal amount misrendered: %',
      public._amose_format_naira(1500.5);
  end if;
  if public._amose_format_naira(99.99) != '₦99.99' then
    raise exception 'N2: kobo-scale amount misrendered: %',
      public._amose_format_naira(99.99);
  end if;
  if public._amose_format_naira(0) != '₦0.00' then
    raise exception 'N2: zero misrendered: %',
      public._amose_format_naira(0);
  end if;
  if public._amose_format_naira(-250) != '-₦250.00' then
    raise exception 'N2: negative amount misrendered: %',
      public._amose_format_naira(-250);
  end if;
end $$;

-- N2b. Grouped amount inside a full sale message.
do $$
declare
  v_text text;
begin
  v_text := public._amose_review_request_text(
    'sale', 'probe-shop', 'main',
    '{"fields": {"quantity": 2, "unit": "bag", "unit_price": 1250.50}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%2 bag at ₦1,250.50%' then
    raise exception 'N2b: grouped sale misrendered: %', v_text;
  end if;
end $$;

-- N3. Null/missing optional money fields fall back to '?', never crash.
do $$
declare
  v_text text;
begin
  if public._amose_format_naira(null) is not null then
    raise exception 'N3: null did not yield null';
  end if;
  v_text := public._amose_review_request_text(
    'sale', 'probe-shop', 'main',
    '{"fields": {"quantity": 2, "unit": "bag"}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%2 bag at ?%' then
    raise exception 'N3: missing unit_price has no fallback: %', v_text;
  end if;
  v_text := public._amose_review_request_text(
    'payment', 'probe-shop', 'main',
    '{"fields": {"method": "transfer"}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%amount ?%' then
    raise exception 'N3: missing payment amount has no fallback: %', v_text;
  end if;
end $$;

-- N4. Payment and expense money formatting.
do $$
declare
  v_text text;
begin
  v_text := public._amose_review_request_text(
    'payment', 'probe-shop', 'main',
    '{"fields": {"amount_kobo": 2500, "method": "transfer"}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%amount ₦2,500.00, method transfer%' then
    raise exception 'N4: payment misrendered: %', v_text;
  end if;
  v_text := public._amose_review_request_text(
    'expense', 'probe-shop', 'main',
    '{"fields": {"amount_kobo": 99.99, "category": "fuel"}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%fuel, amount ₦99.99%' then
    raise exception 'N4: expense misrendered: %', v_text;
  end if;
end $$;

-- N5. No regression to quantities, units, or non-money kinds.
do $$
declare
  v_text text;
begin
  v_text := public._amose_review_request_text(
    'sale', 'probe-shop', 'main',
    '{"fields": {"quantity": 3, "unit": "crate", "unit_price": 12345.5}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%3 crate at ₦12,345.50%' then
    raise exception 'N5: sale quantity/unit regressed: %', v_text;
  end if;
  v_text := public._amose_review_request_text(
    'production', 'probe-shop', 'main',
    '{"fields": {"good_quantity": 26, "rejected_quantity": 1, "production_date": "2026-09-19"}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%good 26, rejected 1, date 2026-09-19%' then
    raise exception 'N5: production regressed: %', v_text;
  end if;
  v_text := public._amose_review_request_text(
    'poultry_daily_report', 'probe-shop', 'main',
    '{"fields": {"good_quantity": 10, "production_date": "2026-09-18"}}',
    'YR-AAAAAAAAAA');
  if v_text not like '%good 10%' then
    raise exception 'N5: poultry report regressed: %', v_text;
  end if;
end $$;

rollback;

do $$ begin
  raise notice 'ALL NAIRA FORMAT PROBES PASSED';
end $$;
