-- Scope-CHECK probes for the branch_clarification migration.
--
-- NOT a migration: this file is never applied by `supabase db reset`.
-- Run it explicitly against the LOCAL database after a reset:
--
--   supabase db reset --local
--   supabase db execute --local -f supabase/probes/branch_clarification_probes.sql
--
-- Proves the explicit clarification scope rule on biz_outbound_messages:
--   1. branch_clarification requires BOTH scope columns NULL;
--   2. every other message type requires BOTH scope columns populated;
--   3. half-null scope (either direction) is rejected for both families.
-- Fixtures satisfy every foreign key, so only the scope CHECK can fire.
-- Every probe is self-validating: any failed assertion raises, aborting the
-- run. A clean run ends with the 'ALL BRANCH CLARIFICATION PROBES PASSED'
-- notice. All writes are rolled back regardless.

begin;

insert into public.biz_tenants (id, name) values
  ('00000000-0000-0000-0000-00000000cc01', 'probe-tenant-clarify');

insert into public.biz_businesses (tenant_id, id, name, business_type) values
  ('00000000-0000-0000-0000-00000000cc01', 'probe-shop', 'Probe Shop', 'retail_store');

insert into public.biz_branches (tenant_id, business_id, id, name) values
  ('00000000-0000-0000-0000-00000000cc01', 'probe-shop', 'main', 'Main');

insert into public.biz_employees (tenant_id, id, display_name, active) values
  ('00000000-0000-0000-0000-00000000cc01', '00000000-0000-0000-0000-00000000cc02', 'Probe Reviewer', true);

-- C1. Clarification with both scope columns NULL inserts fine.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', null, null,
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'branch_clarification', 'Please begin...', 'queued',
     'probe-clarify-1');
end $$;

-- C2. Clarification with only business_id set is rejected.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', 'probe-shop', null,
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'branch_clarification', 'Please begin...', 'queued',
     'probe-clarify-2');
  raise exception 'C2: business-only clarification was accepted';
exception when check_violation then
  if sqlerrm not like '%clarification_scope_check%' then
    raise exception 'C2: wrong constraint fired: %', sqlerrm;
  end if;
end $$;

-- C3. Clarification with only branch_id set is rejected.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', null, 'main',
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'branch_clarification', 'Please begin...', 'queued',
     'probe-clarify-3');
  raise exception 'C3: branch-only clarification was accepted';
exception when check_violation then
  if sqlerrm not like '%clarification_scope_check%' then
    raise exception 'C3: wrong constraint fired: %', sqlerrm;
  end if;
end $$;

-- C4. Clarification with both scope columns set is rejected.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', 'probe-shop', 'main',
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'branch_clarification', 'Please begin...', 'queued',
     'probe-clarify-4');
  raise exception 'C4: scoped clarification was accepted';
exception when check_violation then
  if sqlerrm not like '%clarification_scope_check%' then
    raise exception 'C4: wrong constraint fired: %', sqlerrm;
  end if;
end $$;

-- C5. A non-clarification type with both scope columns NULL is rejected.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', null, null,
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'review_request', 'Review request...', 'queued',
     'probe-clarify-5');
  raise exception 'C5: scopeless review_request was accepted';
exception when check_violation then
  if sqlerrm not like '%clarification_scope_check%' then
    raise exception 'C5: wrong constraint fired: %', sqlerrm;
  end if;
end $$;

-- C6. A non-clarification type with half-null scope is rejected.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', 'probe-shop', null,
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'review_request', 'Review request...', 'queued',
     'probe-clarify-6');
  raise exception 'C6: half-null review_request was accepted';
exception when check_violation then
  if sqlerrm not like '%clarification_scope_check%' then
    raise exception 'C6: wrong constraint fired: %', sqlerrm;
  end if;
end $$;

-- C7. A non-clarification type with full scope still inserts fine.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, message_type, message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000cc01', 'probe-shop', 'main',
     '00000000-0000-0000-0000-00000000cc02', 'whatsapp',
     '+12025550123', 'review_request', 'Review request...', 'queued',
     'probe-clarify-7');
end $$;

rollback;

do $$ begin
  raise notice 'ALL BRANCH CLARIFICATION PROBES PASSED';
end $$;
