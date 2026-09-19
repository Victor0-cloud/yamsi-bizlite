-- Fix review-request naira formatting (Telegram + WhatsApp share it).
--
-- Root cause: public._amose_format_kobo divided its input by 100, but every
-- caller passes NAIRA amounts -- parsed intake stores naira directly
-- (message_processor parse_message: unit_price = float("500") for
-- "Sold 1 bag at 500 NGN"; calculations use quantity * unit_price as naira;
-- confirm builds unit_price_kobo from the same naira value). A sale at 500
-- therefore rendered as "₦5.00" instead of "₦500.00".
--
-- Forward-only fix (applied migrations are never edited):
--   1. New public._amose_format_naira(numeric): renders a naira amount as
--      "₦<whole>.<2 digits>", rounding to 2 decimals for display. Storage
--      and accounting semantics are untouched -- this is display-only.
--   2. public._amose_review_request_text is replaced with an identical body
--      except its three money call sites (sale unit_price, payment amount,
--      expense amount) now use _amose_format_naira. Quantities, units,
--      methods, categories, references, and instructions are byte-identical.
--   3. public._amose_format_kobo is left in place, untouched: it is correct
--      for genuine kobo inputs and nothing calls it after this migration.
--   4. Least privilege preserved: EXECUTE on the new helper is revoked from
--      every role including service_role (owner-only, like its sibling).
--      CREATE OR REPLACE preserves the existing revokes on
--      _amose_review_request_text.
begin;

create or replace function public._amose_format_naira(p_amount numeric)
returns text
language plpgsql
immutable
security definer
set search_path = pg_catalog
as $func$
declare
  v_rounded numeric;
  v_sign text := '';
  v_abs numeric;
  v_whole bigint;
  v_frac int;
  v_digits text;
  v_grouped text := '';
begin
  -- Display-only naira rendering. NULL, NaN, and out-of-range inputs yield
  -- NULL so callers fall back to '?' instead of printing a wrong amount.
  if p_amount is null or p_amount != p_amount then
    return null;
  end if;
  v_rounded := pg_catalog.round(p_amount, 2);
  if v_rounded < -999999999999999.99
      or v_rounded > 999999999999999.99 then
    return null;
  end if;
  if v_rounded < 0 then
    v_sign := '-';
    v_abs := -v_rounded;
  else
    v_abs := v_rounded;
  end if;
  v_whole := pg_catalog.trunc(v_abs)::bigint;
  v_frac := ((v_abs - pg_catalog.trunc(v_abs)) * 100)::int;
  -- Thousands grouping on the whole-naira part only (deterministic commas,
  -- independent of server locale).
  v_digits := v_whole::text;
  while pg_catalog.length(v_digits) > 3 loop
    v_grouped := ',' || pg_catalog.right(v_digits, 3) || v_grouped;
    v_digits := pg_catalog.left(v_digits,
      pg_catalog.length(v_digits) - 3);
  end loop;
  v_grouped := v_digits || v_grouped;
  return v_sign || '₦' || v_grouped || '.'
    || pg_catalog.lpad(v_frac::text, 2, '0');
end;
$func$;

revoke execute on function public._amose_format_naira(numeric)
  from public, anon, authenticated, service_role;

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
      then public._amose_format_naira(v_raw::numeric) else null end;
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
      then public._amose_format_naira(v_raw::numeric) else null end;
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
      then public._amose_format_naira(v_raw::numeric) else null end;
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

commit;
