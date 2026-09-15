-- Allow the private backend to read the business directory only.
-- Does not grant access to anonymous or signed-in frontend users.
begin;
grant usage on schema public to service_role;
grant select on public.biz_businesses, public.biz_branches to service_role;
commit;
