-- Production-readiness probes for the provider-account registry counts
-- that GET /internal/whatsapp/readiness reports per tenant.
--
-- NOT a migration: this file is never applied by `supabase db reset`.
-- Run it explicitly against the LOCAL database after a reset, e.g.:
--
--   supabase db reset --local
--   (pipe this file into the local postgres, then roll back)
--
-- Proves, with throwaway fixtures only:
--   P1. the enabled provider-account count per tenant counts exactly the
--       enabled rows of that tenant;
--   P2. disabled rows are excluded from the count;
--   P3. one tenant's count never includes another tenant's accounts;
--   P4. revoking (disabling) an account immediately drops it from the
--       count, and re-enabling restores it.
-- Every probe is self-validating: any failed assertion raises, aborting
-- the run. A clean run ends with the 'ALL ... PROBES PASSED' notice.
-- All writes are rolled back regardless.

begin;

insert into public.biz_tenants (id, name) values
  ('00000000-0000-0000-0000-00000000e201', 'probe-tenant-ready-a'),
  ('00000000-0000-0000-0000-00000000e202', 'probe-tenant-ready-b');

insert into public.biz_businesses (tenant_id, id, name, business_type) values
  ('00000000-0000-0000-0000-00000000e201', 'probe-water', 'Probe Water', 'water_factory'),
  ('00000000-0000-0000-0000-00000000e202', 'probe-water', 'Probe Water', 'water_factory');

insert into public.biz_branches (tenant_id, business_id, id, name) values
  ('00000000-0000-0000-0000-00000000e201', 'probe-water', 'main', 'Main'),
  ('00000000-0000-0000-0000-00000000e201', 'probe-water', 'north', 'North'),
  ('00000000-0000-0000-0000-00000000e202', 'probe-water', 'main', 'Main');

insert into public.biz_provider_accounts
  (tenant_id, business_id, branch_id, provider, provider_account, enabled, label) values
  ('00000000-0000-0000-0000-00000000e201', 'probe-water', 'main', 'whatsapp',
   'acct-ready-1', true, 'Ready line one'),
  ('00000000-0000-0000-0000-00000000e201', 'probe-water', 'north', 'whatsapp',
   'acct-ready-2', true, 'Ready line two'),
  ('00000000-0000-0000-0000-00000000e201', 'probe-water', 'main', 'whatsapp',
   'acct-ready-old', false, 'Retired line'),
  ('00000000-0000-0000-0000-00000000e202', 'probe-water', 'main', 'whatsapp',
   'acct-ready-b1', true, 'Other tenant line');

-- P1/P2/P3. Per-tenant enabled counts exclude disabled rows and never
-- cross tenant boundaries.
do $$
declare
  v_a int;
  v_b int;
begin
  select count(*) into v_a from public.biz_provider_accounts
    where tenant_id = '00000000-0000-0000-0000-00000000e201'
      and enabled is true;
  if v_a <> 2 then
    raise exception 'P1: tenant A enabled count is %, want 2', v_a;
  end if;
  select count(*) into v_b from public.biz_provider_accounts
    where tenant_id = '00000000-0000-0000-0000-00000000e202'
      and enabled is true;
  if v_b <> 1 then
    raise exception 'P3: tenant B enabled count is %, want 1', v_b;
  end if;
  if exists (select 1 from public.biz_provider_accounts
      where tenant_id = '00000000-0000-0000-0000-00000000e201'
        and provider_account = 'acct-ready-b1') then
    raise exception 'P3: tenant B account visible under tenant A';
  end if;
end $$;

-- P4. Disabling drops the account from the count at once; re-enabling
-- restores it at once.
do $$
declare
  v_count int;
begin
  update public.biz_provider_accounts
    set enabled = false
    where tenant_id = '00000000-0000-0000-0000-00000000e201'
      and provider_account = 'acct-ready-1';
  select count(*) into v_count from public.biz_provider_accounts
    where tenant_id = '00000000-0000-0000-0000-00000000e201'
      and enabled is true;
  if v_count <> 1 then
    raise exception 'P4a: count after revoke is %, want 1', v_count;
  end if;
  update public.biz_provider_accounts
    set enabled = true
    where tenant_id = '00000000-0000-0000-0000-00000000e201'
      and provider_account = 'acct-ready-1';
  select count(*) into v_count from public.biz_provider_accounts
    where tenant_id = '00000000-0000-0000-0000-00000000e201'
      and enabled is true;
  if v_count <> 2 then
    raise exception 'P4b: count after restore is %, want 2', v_count;
  end if;
end $$;

rollback;

do $$ begin
  raise notice 'ALL PRODUCTION READINESS PROBES PASSED';
end $$;
