-- Deposit approval-gate probes for the daily-business-records migration.
--
-- NOT a migration: this file is never applied by `supabase db reset`.
-- Run it explicitly against the LOCAL database after a reset:
--
--   supabase db reset --local
--   supabase db execute --local -f supabase/probes/daily_business_records_probes.sql
--
-- Proves the owner-approved destination-account gate inside
-- public.amose_post_bank_deposit:
--   D1. an approved account posts, storing the canonical approved name;
--   D2. an unapproved account fails closed (APPROVAL);
--   D3. a scope with no setting fails closed (APPROVAL);
--   D4. malformed/empty settings fail closed (APPROVAL);
--   D5. case/whitespace variants match and store the canonical name;
--   D6. a duplicate reference cannot bypass detection through
--       case/whitespace (CONFLICT);
--   D7. the same reference for a different destination account is a
--       different deposit (scoped duplicate prevention);
--   D8. an identical retry returns already_posted without duplicating.
-- Fixtures satisfy every foreign key, so only the intended errors fire.
-- Every probe is self-validating: any failed assertion raises, aborting
-- the run. A clean run ends with the 'ALL ... PROBES PASSED' notice.
-- All writes are rolled back regardless.

begin;

insert into public.biz_tenants (id, name) values
  ('00000000-0000-0000-0000-00000000d101', 'probe-tenant-deposits');

insert into public.biz_businesses (tenant_id, id, name, business_type) values
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'Probe Water', 'water_factory');

insert into public.biz_branches (tenant_id, business_id, id, name) values
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main', 'Main'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'north', 'North');

insert into public.biz_employees (tenant_id, id, display_name, active) values
  ('00000000-0000-0000-0000-00000000d101', '00000000-0000-0000-0000-00000000d102', 'Probe Depositor', true),
  ('00000000-0000-0000-0000-00000000d101', '00000000-0000-0000-0000-00000000d103', 'Probe Reporter', true);

insert into public.biz_assignments (tenant_id, employee_id, business_id, branch_id, role) values
  ('00000000-0000-0000-0000-00000000d101', '00000000-0000-0000-0000-00000000d102', 'probe-water', 'main', 'worker'),
  ('00000000-0000-0000-0000-00000000d101', '00000000-0000-0000-0000-00000000d103', 'probe-water', 'main', 'worker'),
  ('00000000-0000-0000-0000-00000000d101', '00000000-0000-0000-0000-00000000d102', 'probe-water', 'north', 'worker');

insert into public.biz_setting_versions
  (tenant_id, business_id, branch_id, key, effective_from, value) values
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   'approved_bank_deposit_accounts', now() - interval '1 day',
   '{"accounts": [{"name": "Access Bank", "reference": "ACC-01"}, {"name": "First Bank"}]}');

-- One confirmed deposit submission per probe case (verified blocks carry
-- staff-supplied values; approval resolves inside the RPC).
insert into public.biz_submissions
  (tenant_id, business_id, branch_id, id, employee_id,
   idempotency_key, kind, payload, status) values
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000d111',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d111', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "Access Bank",
      "reference": "DEP-001"}}',
   'confirmed'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000d112',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d112', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "Unknown Microfinance",
      "reference": "DEP-002"}}',
   'confirmed'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'north',
   '00000000-0000-0000-0000-00000000d113',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d113', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "Access Bank",
      "reference": "DEP-003"}}',
   'confirmed'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000d114',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d114', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "Access Bank",
      "reference": "DEP-004"}}',
   'confirmed'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000d115',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d115', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "  access BANK ",
      "reference": "DEP-005"}}',
   'confirmed'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000d116',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d116', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "Access Bank",
      "reference": " dep-001 "}}',
   'confirmed'),
  ('00000000-0000-0000-0000-00000000d101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000d117',
   '00000000-0000-0000-0000-00000000d103',
   'probe:d117', 'bank_deposit',
   '{"verified": {"deposited_by": "00000000-0000-0000-0000-00000000d102",
      "amount_kobo": 7000000, "destination_account": "First Bank",
      "reference": "DEP-001"}}',
   'confirmed');

-- D3 needs a reporter assignment in north (the depositor already has one).
insert into public.biz_assignments (tenant_id, employee_id, business_id, branch_id, role) values
  ('00000000-0000-0000-0000-00000000d101', '00000000-0000-0000-0000-00000000d103', 'probe-water', 'north', 'worker');

-- D1. Approved account posts, storing the canonical approved name.
do $$
declare
  v_result jsonb;
  v_dest text;
begin
  select public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d111') into v_result;
  if v_result ->> 'status' <> 'posted' then
    raise exception 'D1: expected posted, got %', v_result;
  end if;
  if v_result ->> 'posting_type' <> 'bank_deposit' then
    raise exception 'D1: wrong posting type %', v_result;
  end if;
  select c.destination_account into v_dest
    from public.biz_cash_custody_entries c
    where c.idempotency_key = 'posting:00000000-0000-0000-0000-00000000d111:bank_deposit';
  if v_dest is distinct from 'Access Bank' then
    raise exception 'D1: canonical destination not stored, got %', v_dest;
  end if;
end $$;

-- D2. Unapproved account fails closed.
do $$
begin
  perform public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d112');
  raise exception 'D2: unapproved account was accepted';
exception when others then
  if sqlerrm not like 'APPROVAL:%not an approved bank deposit account%' then
    raise exception 'D2: wrong error: %', sqlerrm;
  end if;
end $$;

-- D3. A scope with no setting fails closed.
do $$
begin
  perform public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d113');
  raise exception 'D3: missing setting was accepted';
exception when others then
  if sqlerrm not like 'APPROVAL:%no approved bank deposit accounts are configured%' then
    raise exception 'D3: wrong error: %', sqlerrm;
  end if;
end $$;

-- D4a. Empty accounts array fails closed.
update public.biz_setting_versions
  set value = '{"accounts": []}'
  where tenant_id = '00000000-0000-0000-0000-00000000d101'
    and business_id = 'probe-water' and branch_id = 'main'
    and key = 'approved_bank_deposit_accounts';
do $$
begin
  perform public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d114');
  raise exception 'D4a: empty setting was accepted';
exception when others then
  if sqlerrm not like 'APPROVAL:%no approved bank deposit accounts are configured%' then
    raise exception 'D4a: wrong error: %', sqlerrm;
  end if;
end $$;

-- D4b. Non-object value fails closed.
update public.biz_setting_versions
  set value = '[]'
  where tenant_id = '00000000-0000-0000-0000-00000000d101'
    and business_id = 'probe-water' and branch_id = 'main'
    and key = 'approved_bank_deposit_accounts';
do $$
begin
  perform public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d114');
  raise exception 'D4b: malformed setting was accepted';
exception when others then
  if sqlerrm not like 'APPROVAL:%no approved bank deposit accounts are configured%' then
    raise exception 'D4b: wrong error: %', sqlerrm;
  end if;
end $$;
update public.biz_setting_versions
  set value = '{"accounts": [{"name": "Access Bank", "reference": "ACC-01"}, {"name": "First Bank"}]}'
  where tenant_id = '00000000-0000-0000-0000-00000000d101'
    and business_id = 'probe-water' and branch_id = 'main'
    and key = 'approved_bank_deposit_accounts';

-- D5. Case/whitespace variant matches and stores the canonical name.
do $$
declare
  v_result jsonb;
  v_dest text;
begin
  select public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d115') into v_result;
  if v_result ->> 'status' <> 'posted' then
    raise exception 'D5: expected posted, got %', v_result;
  end if;
  select c.destination_account into v_dest
    from public.biz_cash_custody_entries c
    where c.idempotency_key = 'posting:00000000-0000-0000-0000-00000000d115:bank_deposit';
  if v_dest is distinct from 'Access Bank' then
    raise exception 'D5: canonical destination not stored, got %', v_dest;
  end if;
end $$;

-- D6. Duplicate reference with different case/whitespace is still a duplicate.
do $$
begin
  perform public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d116');
  raise exception 'D6: duplicate reference bypass was accepted';
exception when others then
  if sqlerrm not like 'CONFLICT:%already recorded%' then
    raise exception 'D6: wrong error: %', sqlerrm;
  end if;
end $$;

-- D7. Same reference for a different destination account is a different deposit.
do $$
declare
  v_result jsonb;
begin
  select public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d117') into v_result;
  if v_result ->> 'status' <> 'posted' then
    raise exception 'D7: expected posted, got %', v_result;
  end if;
end $$;

-- D8. Identical retry returns already_posted without duplicating.
do $$
declare
  v_result jsonb;
  v_count int;
begin
  select public.amose_post_bank_deposit('00000000-0000-0000-0000-00000000d111') into v_result;
  if v_result ->> 'status' <> 'already_posted' then
    raise exception 'D8: expected already_posted, got %', v_result;
  end if;
  select count(*) into v_count
    from public.biz_cash_custody_entries c
    where c.idempotency_key = 'posting:00000000-0000-0000-0000-00000000d111:bank_deposit';
  if v_count <> 1 then
    raise exception 'D8: duplicate custody rows: %', v_count;
  end if;
end $$;

rollback;

do $$ begin
  raise notice 'ALL DAILY BUSINESS RECORDS PROBES PASSED';
end $$;
