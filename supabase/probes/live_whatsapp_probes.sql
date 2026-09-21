-- Live WhatsApp integration probes for the 20260921 migration.
--
-- NOT a migration: this file is never applied by `supabase db reset`.
-- Run it explicitly against the LOCAL database after a reset, e.g.:
--
--   supabase db reset --local
--   (pipe this file into the local postgres, then roll back)
--
-- Proves, with throwaway fixtures only:
--   R1. one account may serve many scopes (second scope accepted,
--       exact duplicate scope rejected);
--   R2. a submission with an unregistered account is rejected;
--   R3. a submission with a disabled account is rejected;
--   R4. a submission with no route snapshot is accepted (legacy/API rows);
--   R5. a submission with a cross-scope account is rejected;
--   R6. an outbound claim (queued -> sending) on an authorized account
--       succeeds and stamps the lease;
--   R7. claiming on a disabled account is refused;
--   R8. a stale 'sending' claim is reclaimed transactionally, a second
--       call finds nothing, and bad windows are refused;
--   R9. delivery states progress sent -> delivered -> read monotonically;
--   R10. duplicates and out-of-order events never regress state;
--   R11. failed-over-sent applies with a sanitized code, failed-over-read
--       does not, unknown ids and cross-account ids change nothing;
--   R12. new RPCs/registry/helpers are service-role-only with RLS on.
--   C0. a scoped insert on a disabled account cannot queue;
--   C1. a pre-scope clarification stores (real recipient, null scope);
--   C2/C3. unknown/disabled accounts cannot queue a clarification;
--   C4. a sender-less clarification violates the shape check;
--   C5. an unrelated type cannot use the nullable shape;
--   C6. a duplicate clarification key cannot create a second row;
--   C7. clarification claims succeed, then refuse once disabled.
--   R13. catalog volatility: table-reading helpers are STABLE (never
--       IMMUTABLE); guards and RPCs stay VOLATILE.
--   R14. toggling enabled flips both helpers immediately, blocks and
--       restores submission + claiming in the same instant, twice in a
--       row -- an IMMUTABLE cached result would return the stale value.
-- Every probe is self-validating: any failed assertion raises, aborting
-- the run. A clean run ends with the 'ALL ... PROBES PASSED' notice.
-- All writes are rolled back regardless.

begin;

insert into public.biz_tenants (id, name) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-tenant-live');

insert into public.biz_businesses (tenant_id, id, name, business_type) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'Probe Water', 'water_factory');

insert into public.biz_branches (tenant_id, business_id, id, name) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main', 'Main'),
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'north', 'North');

insert into public.biz_employees (tenant_id, id, display_name, active) values
  ('00000000-0000-0000-0000-00000000e101', '00000000-0000-0000-0000-00000000e102', 'Probe Staffer', true);

insert into public.biz_assignments (tenant_id, employee_id, business_id, branch_id, role) values
  ('00000000-0000-0000-0000-00000000e101', '00000000-0000-0000-0000-00000000e102', 'probe-water', 'main', 'worker');

insert into public.biz_provider_accounts
  (tenant_id, business_id, branch_id, provider, provider_account, enabled, label) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main', 'whatsapp',
   'acct-live-1', true, 'Main line'),
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main', 'whatsapp',
   'acct-old-9', false, 'Retired line'),
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main', 'whatsapp',
   'acct-main-only', true, 'Main-only line');

-- R1. The same account may serve a second scope (one row per scope);
-- an exact duplicate scope is still rejected. The north row stays for
-- the probes below, so the tenant-level helper below also proves
-- multi-row existence semantics.
do $$
begin
  insert into public.biz_provider_accounts
    (tenant_id, business_id, branch_id, provider, provider_account, enabled) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'north', 'whatsapp',
     'acct-live-1', true);
exception when others then
  raise exception 'R1a: cross-scope account was refused: %', sqlerrm;
end $$;
do $$
begin
  insert into public.biz_provider_accounts
    (tenant_id, business_id, branch_id, provider, provider_account, enabled) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main', 'whatsapp',
     'acct-live-1', true);
  raise exception 'R1b: exact duplicate scope was accepted';
exception when unique_violation then
  if sqlerrm not like '%biz_provider_accounts%' then
    raise exception 'R1b: wrong unique violation: %', sqlerrm;
  end if;
end $$;

-- R2. Submission with an unregistered account is rejected.
do $$
begin
  insert into public.biz_submissions
    (tenant_id, business_id, branch_id, id, employee_id,
     idempotency_key, kind, payload, status, provider, provider_account) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
     '00000000-0000-0000-0000-00000000e111',
     '00000000-0000-0000-0000-00000000e102',
     'probe:r2', 'sale', '{"parsed": {}}', 'draft', 'whatsapp', 'acct-ghost');
  raise exception 'R2: unregistered account was accepted';
exception when others then
  if sqlerrm not like '%acct-ghost%' and sqlerrm not like '%provider account%' then
    raise exception 'R2: wrong error: %', sqlerrm;
  end if;
end $$;

-- R3. Submission with a disabled account is rejected.
do $$
begin
  insert into public.biz_submissions
    (tenant_id, business_id, branch_id, id, employee_id,
     idempotency_key, kind, payload, status, provider, provider_account) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
     '00000000-0000-0000-0000-00000000e112',
     '00000000-0000-0000-0000-00000000e102',
     'probe:r3', 'sale', '{"parsed": {}}', 'draft', 'whatsapp', 'acct-old-9');
  raise exception 'R3: disabled account was accepted';
exception when others then
  if sqlerrm not like '%UNAUTHORIZED%' then
    raise exception 'R3: wrong error: %', sqlerrm;
  end if;
end $$;

-- R4. Submission with no route snapshot is accepted.
insert into public.biz_submissions
  (tenant_id, business_id, branch_id, id, employee_id,
   idempotency_key, kind, payload, status) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000e113',
   '00000000-0000-0000-0000-00000000e102',
   'probe:r4', 'sale', '{"parsed": {}}', 'draft');

-- R4b. Submission with the authorized account is accepted.
insert into public.biz_submissions
  (tenant_id, business_id, branch_id, id, employee_id,
   idempotency_key, kind, payload, status, provider, provider_account) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000e114',
   '00000000-0000-0000-0000-00000000e102',
   'probe:r4b', 'sale', '{"parsed": {}}', 'draft', 'whatsapp', 'acct-live-1');

-- R5. Submission binding a main-only account to another branch is
-- rejected (acct-live-1 now legitimately serves north too, so a
-- main-only account proves misbinding still fails closed).
do $$
begin
  insert into public.biz_submissions
    (tenant_id, business_id, branch_id, id, employee_id,
     idempotency_key, kind, payload, status, provider, provider_account) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'north',
     '00000000-0000-0000-0000-00000000e115',
     '00000000-0000-0000-0000-00000000e102',
     'probe:r5', 'sale', '{"parsed": {}}', 'draft', 'whatsapp', 'acct-main-only');
  raise exception 'R5: cross-scope account was accepted';
exception when others then
  if sqlerrm not like '%acct-main-only%' and sqlerrm not like '%provider account%' then
    raise exception 'R5: wrong error: %', sqlerrm;
  end if;
end $$;

-- Outbound fixtures: one authorized review row and one scoped legacy row
-- with no account snapshot (message types satisfy the pre-existing
-- review/clarification scope checks).
insert into public.biz_outbound_messages
  (tenant_id, business_id, branch_id, recipient_employee_id, provider,
   provider_sender, provider_account, related_task_id, message_type,
   message_text, status, idempotency_key) values
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000e102', 'whatsapp',
   '+12025550123', 'acct-live-1', null, 'review_request',
   'Please review this report.', 'queued', 'probe:o1'),
  ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
   '00000000-0000-0000-0000-00000000e102', 'whatsapp',
   '+12025550123', null, null, 'review_request',
   'Please review this report.', 'queued', 'probe:o2');

-- R6. Claim on the authorized account succeeds and stamps the lease.
do $$
declare
  v_status text;
  v_lease timestamptz;
begin
  update public.biz_outbound_messages o
    set status = 'sending', claimed_at = now()
    where o.idempotency_key = 'probe:o1' and o.status = 'queued'
    returning o.status, o.claimed_at into v_status, v_lease;
  if v_status is distinct from 'sending' or v_lease is null then
    raise exception 'R6: claim did not apply';
  end if;
end $$;

-- R7. Claiming on a disabled account is refused.
do $$
begin
  update public.biz_outbound_messages o
    set status = 'sending'
    where o.idempotency_key = 'probe:o2';
  update public.biz_outbound_messages o
    set provider_account = 'acct-old-9', business_id = 'probe-water',
      branch_id = 'main', message_type = 'review_request',
      related_task_id = null, status = 'queued'
    where o.idempotency_key = 'probe:o2';
  update public.biz_outbound_messages o
    set status = 'sending'
    where o.idempotency_key = 'probe:o2' and o.status = 'queued';
  raise exception 'R7: disabled-account claim was accepted';
exception when others then
  if sqlerrm not like '%UNAUTHORIZED%' then
    raise exception 'R7: wrong error: %', sqlerrm;
  end if;
end $$;

-- R8. Stale-claim recovery re-queues exactly once and validates input.
update public.biz_outbound_messages o
  set claimed_at = now() - interval '2 hours'
  where o.idempotency_key = 'probe:o1';
do $$
declare
  v_result jsonb;
begin
  select public.amose_reclaim_stale_outbound(3600, 50) into v_result;
  if (v_result ->> 'reclaimed')::int <> 1 then
    raise exception 'R8: expected 1 reclaimed, got %', v_result;
  end if;
  select public.amose_reclaim_stale_outbound(3600, 50) into v_result;
  if (v_result ->> 'reclaimed')::int <> 0 then
    raise exception 'R8: second call was not a no-op: %', v_result;
  end if;
end $$;
do $$
begin
  perform public.amose_reclaim_stale_outbound(10, 50);
  raise exception 'R8: bad window was accepted';
exception when others then
  if sqlerrm not like '%MALFORMED%' then
    raise exception 'R8: wrong error: %', sqlerrm;
  end if;
end $$;

-- Delivery fixtures: a sent row with a provider message id.
update public.biz_outbound_messages o
  set status = 'sent', provider_message_id = 'wamid-probe-1'
  where o.idempotency_key = 'probe:o1';

-- R9. Monotonic progress sent -> delivered -> read.
do $$
declare
  v_result jsonb;
begin
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'sent', null) into v_result;
  if (v_result ->> 'applied')::boolean is not true then
    raise exception 'R9a: first event did not apply: %', v_result;
  end if;
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'delivered', null) into v_result;
  if v_result ->> 'current' <> 'delivered' then
    raise exception 'R9b: %', v_result;
  end if;
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'read', null) into v_result;
  if v_result ->> 'current' <> 'read' then
    raise exception 'R9c: %', v_result;
  end if;
end $$;

-- R10. Duplicates and out-of-order events never regress.
do $$
declare
  v_result jsonb;
begin
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'delivered', null) into v_result;
  if (v_result ->> 'applied')::boolean is true
      or v_result ->> 'reason' <> 'stale_or_duplicate' then
    raise exception 'R10a: regression applied: %', v_result;
  end if;
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'read', null) into v_result;
  if (v_result ->> 'applied')::boolean is true
      or v_result ->> 'reason' <> 'stale_or_duplicate' then
    raise exception 'R10b: duplicate was not harmless: %', v_result;
  end if;
  if (select o.delivery_state from public.biz_outbound_messages o
      where o.idempotency_key = 'probe:o1') <> 'read' then
    raise exception 'R10c: state moved';
  end if;
end $$;

-- R11. Failed-over-sent applies sanitized; failed-over-read, unknown ids,
-- cross-account ids, and bad states do not.
update public.biz_outbound_messages o
  set delivery_state = 'sent', delivery_updated_at = null,
    delivery_error = null
  where o.idempotency_key = 'probe:o1';
do $$
declare
  v_result jsonb;
  v_error text;
begin
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'failed', '131026:Message undeliverable now!!!') into v_result;
  if (v_result ->> 'applied')::boolean is not true then
    raise exception 'R11a: failed-over-sent did not apply: %', v_result;
  end if;
  select o.delivery_error into v_error
    from public.biz_outbound_messages o
    where o.idempotency_key = 'probe:o1';
  if v_error is distinct from '131026:Message undeliverable now!!!' then
    raise exception 'R11b: error not sanitized-stored: %', v_error;
  end if;
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'delivered', null) into v_result;
  if (v_result ->> 'applied')::boolean is true then
    raise exception 'R11c: moved over failed: %', v_result;
  end if;
  select public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-unknown-9', 'delivered', null) into v_result;
  if (v_result ->> 'applied')::boolean is true
      or v_result ->> 'reason' <> 'unknown_message' then
    raise exception 'R11d: unknown id modified a row: %', v_result;
  end if;
  select public.amose_apply_delivery_status('whatsapp', 'acct-other-9',
    'wamid-probe-1', 'delivered', null) into v_result;
  if (v_result ->> 'applied')::boolean is true
      or v_result ->> 'reason' <> 'unknown_message' then
    raise exception 'R11e: cross-account id modified a row: %', v_result;
  end if;
end $$;
do $$
begin
  perform public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'teleported', null);
  raise exception 'R11f: bad state was accepted';
exception when others then
  if sqlerrm not like '%MALFORMED%' then
    raise exception 'R11f: wrong error: %', sqlerrm;
  end if;
end $$;
do $$
declare
  v_error text;
begin
  update public.biz_outbound_messages o
    set delivery_state = 'sent', delivery_error = null
    where o.idempotency_key = 'probe:o1';
  perform public.amose_apply_delivery_status('whatsapp', 'acct-live-1',
    'wamid-probe-1', 'failed', 'not-a-code-volume-' || repeat('x', 300));
  select o.delivery_error into v_error
    from public.biz_outbound_messages o
    where o.idempotency_key = 'probe:o1';
  if v_error is distinct from 'unknown' then
    raise exception 'R11g: hostile error code stored: %', v_error;
  end if;
end $$;

-- C0. A scoped insert on a disabled account cannot queue either.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     '+12025550123', 'acct-old-9', null, 'review_request',
     'Please review this report.', 'queued', 'probe:c0');
  raise exception 'C0: disabled-account queueing was accepted';
exception when others then
  if sqlerrm not like '%UNAUTHORIZED%' then
    raise exception 'C0: wrong error: %', sqlerrm;
  end if;
end $$;

-- C1. A pre-scope clarification with the real sender recipient, null
-- scope, and a registered account stores successfully.
insert into public.biz_outbound_messages
  (tenant_id, business_id, branch_id, recipient_employee_id, provider,
   provider_sender, provider_account, related_task_id, message_type,
   message_text, status, idempotency_key) values
  ('00000000-0000-0000-0000-00000000e101', null, null,
   '00000000-0000-0000-0000-00000000e102', 'whatsapp',
   '+12025550123', 'acct-live-1', null, 'branch_clarification',
   'Please begin your message with MAIN: or NORTH:.', 'queued', 'probe:c1');
do $$
declare
  v_business text;
  v_recipient uuid;
begin
  select o.business_id, o.recipient_employee_id into v_business, v_recipient
    from public.biz_outbound_messages o
    where o.idempotency_key = 'probe:c1';
  if v_business is not null then
    raise exception 'C1: scope was guessed';
  end if;
  if v_recipient is distinct from '00000000-0000-0000-0000-00000000e102' then
    raise exception 'C1: recipient is not the real sender';
  end if;
end $$;

-- C2. Clarification with an unknown account cannot queue.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', null, null,
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     '+12025550123', 'acct-ghost-9', null, 'branch_clarification',
     'Please begin your message with MAIN: or NORTH:.', 'queued', 'probe:c2');
  raise exception 'C2: unknown-account clarification was accepted';
exception when others then
  if sqlerrm not like '%UNAUTHORIZED%' then
    raise exception 'C2: wrong error: %', sqlerrm;
  end if;
end $$;

-- C3. Clarification with a disabled account cannot queue.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', null, null,
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     '+12025550123', 'acct-old-9', null, 'branch_clarification',
     'Please begin your message with MAIN: or NORTH:.', 'queued', 'probe:c3');
  raise exception 'C3: disabled-account clarification was accepted';
exception when others then
  if sqlerrm not like '%UNAUTHORIZED%' then
    raise exception 'C3: wrong error: %', sqlerrm;
  end if;
end $$;

-- C4. Clarification without a sender violates the shape check.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', null, null,
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     null, 'acct-live-1', null, 'branch_clarification',
     'Please begin your message with MAIN: or NORTH:.', 'queued', 'probe:c4');
  raise exception 'C4: sender-less clarification was accepted';
exception when others then
  if sqlerrm not like '%outbound_clarification_shape_check%' then
    raise exception 'C4: wrong error: %', sqlerrm;
  end if;
end $$;

-- C5. An unrelated type cannot use the clarification-only nullable shape.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', null, null,
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     '+12025550123', 'acct-live-1', null, 'review_request',
     'Please review this report.', 'queued', 'probe:c5');
  raise exception 'C5: scopeless review_request was accepted';
exception when others then
  if sqlerrm not like '%clarification_scope_check%' then
    raise exception 'C5: wrong error: %', sqlerrm;
  end if;
end $$;

-- C6. A duplicate clarification key cannot create a second row.
do $$
begin
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', null, null,
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     '+12025550123', 'acct-live-1', null, 'branch_clarification',
     'Please begin your message with MAIN: or NORTH:.', 'queued', 'probe:c1');
  raise exception 'C6: duplicate clarification was accepted';
exception when unique_violation then
  if sqlerrm not like '%idempotency%' then
    raise exception 'C6: wrong unique violation: %', sqlerrm;
  end if;
end $$;

-- C7. Claiming the clarification succeeds through the original account;
-- disabling the account afterwards refuses the next claim.
do $$
declare
  v_status text;
begin
  update public.biz_outbound_messages o
    set status = 'sending', claimed_at = now()
    where o.idempotency_key = 'probe:c1' and o.status = 'queued'
    returning o.status into v_status;
  if v_status is distinct from 'sending' then
    raise exception 'C7a: clarification claim did not apply';
  end if;
  update public.biz_outbound_messages o
    set status = 'queued', claimed_at = null
    where o.idempotency_key = 'probe:c1';
  update public.biz_provider_accounts a
    set enabled = false
    where a.tenant_id = '00000000-0000-0000-0000-00000000e101'
      and a.provider_account = 'acct-live-1';
  begin
    update public.biz_outbound_messages o
      set status = 'sending'
      where o.idempotency_key = 'probe:c1' and o.status = 'queued';
    raise exception 'C7b: disabled-account clarification claim was accepted';
  exception when others then
    if sqlerrm not like '%UNAUTHORIZED%' then
      raise exception 'C7b: wrong error: %', sqlerrm;
    end if;
  end;
  update public.biz_provider_accounts a
    set enabled = true
    where a.tenant_id = '00000000-0000-0000-0000-00000000e101'
      and a.provider_account = 'acct-live-1';
end $$;

-- R12. Least privilege: new RPCs and registry are service-role-only.
do $$
declare
  v_grants int;
begin
  select count(*) into v_grants
    from information_schema.routine_privileges
    where routine_schema = 'public'
      and routine_name in ('amose_reclaim_stale_outbound',
        'amose_apply_delivery_status')
      and privilege_type = 'EXECUTE'
      and grantee not in ('postgres', 'service_role');
  if v_grants <> 0 then
    raise exception 'R12a: over-broad RPC grant';
  end if;
  select count(*) into v_grants
    from information_schema.role_table_grants
    where table_schema = 'public'
      and table_name = 'biz_provider_accounts'
      and grantee in ('anon', 'authenticated', 'public');
  if v_grants <> 0 then
    raise exception 'R12b: caller-role table grant';
  end if;
  if not (select relrowsecurity from pg_class
      where relnamespace = 'public'::regnamespace
        and relname = 'biz_provider_accounts') then
    raise exception 'R12c: RLS is off';
  end if;
  select count(*) into v_grants
    from information_schema.routine_privileges
    where routine_schema = 'public'
      and routine_name in ('_amose_provider_account_authorized',
        '_amose_provider_tenant_authorized',
        '_amose_guard_submission_provider_account',
        '_amose_guard_outbound_provider_account')
      and grantee <> 'postgres';
  if v_grants <> 0 then
    raise exception 'R12d: helper callable by a caller role';
  end if;
end $$;

-- R13. Volatility contract at the catalog level: helpers that read the
-- registry must be STABLE ('s'), never IMMUTABLE ('i'); trigger guards
-- and writing RPCs must stay VOLATILE ('v').
do $$
declare
  v_vol char;
begin
  select p.provolatile into v_vol from pg_proc p
    where p.pronamespace = 'public'::regnamespace
      and p.proname = '_amose_provider_account_authorized';
  if v_vol is distinct from 's' then
    raise exception 'R13a: scoped helper volatility is %', v_vol;
  end if;
  select p.provolatile into v_vol from pg_proc p
    where p.pronamespace = 'public'::regnamespace
      and p.proname = '_amose_provider_tenant_authorized';
  if v_vol is distinct from 's' then
    raise exception 'R13b: tenant helper volatility is %', v_vol;
  end if;
  for v_vol in
    select p.provolatile from pg_proc p
      where p.pronamespace = 'public'::regnamespace
        and p.proname in ('_amose_guard_submission_provider_account',
          '_amose_guard_outbound_provider_account',
          'amose_reclaim_stale_outbound', 'amose_apply_delivery_status')
  loop
    if v_vol is distinct from 'v' then
      raise exception 'R13c: guard/RPC volatility is %', v_vol;
    end if;
  end loop;
end $$;

-- R14. Authorization tracks the registry row with no caching: disable
-- flips both helpers to false and blocks submission + claim at once;
-- re-enable restores both at once; a second toggle behaves identically.
do $$
declare
  v_scoped boolean;
  v_tenant boolean;
  v_status text;
begin
  select public._amose_provider_account_authorized(
      '00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
      'whatsapp', 'acct-live-1') into v_scoped;
  select public._amose_provider_tenant_authorized(
      '00000000-0000-0000-0000-00000000e101',
      'whatsapp', 'acct-live-1') into v_tenant;
  if v_scoped is not true or v_tenant is not true then
    raise exception 'R14a: enabled account did not authorize';
  end if;
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id, provider,
     provider_sender, provider_account, related_task_id, message_type,
     message_text, status, idempotency_key) values
    ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
     '00000000-0000-0000-0000-00000000e102', 'whatsapp',
     '+12025550123', 'acct-live-1', null, 'review_request',
     'Please review this report.', 'queued', 'probe:r14q');
  update public.biz_provider_accounts a
    set enabled = false
    where a.tenant_id = '00000000-0000-0000-0000-00000000e101'
      and a.provider_account = 'acct-live-1';
  select public._amose_provider_account_authorized(
      '00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
      'whatsapp', 'acct-live-1') into v_scoped;
  select public._amose_provider_tenant_authorized(
      '00000000-0000-0000-0000-00000000e101',
      'whatsapp', 'acct-live-1') into v_tenant;
  if v_scoped is not false or v_tenant is not false then
    raise exception 'R14b: disabled account still authorized (stale cache)';
  end if;
  begin
    insert into public.biz_submissions
      (tenant_id, business_id, branch_id, id, employee_id,
       idempotency_key, kind, payload, status, provider, provider_account) values
      ('00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
       '00000000-0000-0000-0000-00000000e116',
       '00000000-0000-0000-0000-00000000e102',
       'probe:r14', 'sale', '{"parsed": {}}', 'draft', 'whatsapp', 'acct-live-1');
    raise exception 'R14c: disabled-account submission was accepted';
  exception when others then
    if sqlerrm not like '%UNAUTHORIZED%' then
      raise exception 'R14c: wrong error: %', sqlerrm;
    end if;
  end;
  begin
    update public.biz_outbound_messages o
      set status = 'sending'
      where o.idempotency_key = 'probe:r14q' and o.status = 'queued';
    raise exception 'R14d: disabled-account claim was accepted';
  exception when others then
    if sqlerrm not like '%UNAUTHORIZED%' then
      raise exception 'R14d: wrong error: %', sqlerrm;
    end if;
  end;
  update public.biz_provider_accounts a
    set enabled = true
    where a.tenant_id = '00000000-0000-0000-0000-00000000e101'
      and a.provider_account = 'acct-live-1';
  select public._amose_provider_account_authorized(
      '00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
      'whatsapp', 'acct-live-1') into v_scoped;
  select public._amose_provider_tenant_authorized(
      '00000000-0000-0000-0000-00000000e101',
      'whatsapp', 'acct-live-1') into v_tenant;
  if v_scoped is not true or v_tenant is not true then
    raise exception 'R14e: re-enabled account did not authorize';
  end if;
  update public.biz_outbound_messages o
    set status = 'sending', claimed_at = now()
    where o.idempotency_key = 'probe:r14q' and o.status = 'queued'
    returning o.status into v_status;
  if v_status is distinct from 'sending' then
    raise exception 'R14f: restored claim did not apply';
  end if;
  update public.biz_provider_accounts a
    set enabled = false
    where a.tenant_id = '00000000-0000-0000-0000-00000000e101'
      and a.provider_account = 'acct-live-1';
  select public._amose_provider_account_authorized(
      '00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
      'whatsapp', 'acct-live-1') into v_scoped;
  select public._amose_provider_tenant_authorized(
      '00000000-0000-0000-0000-00000000e101',
      'whatsapp', 'acct-live-1') into v_tenant;
  if v_scoped is not false or v_tenant is not false then
    raise exception 'R14g: second disable still authorized (stale cache)';
  end if;
  update public.biz_provider_accounts a
    set enabled = true
    where a.tenant_id = '00000000-0000-0000-0000-00000000e101'
      and a.provider_account = 'acct-live-1';
  select public._amose_provider_account_authorized(
      '00000000-0000-0000-0000-00000000e101', 'probe-water', 'main',
      'whatsapp', 'acct-live-1') into v_scoped;
  if v_scoped is not true then
    raise exception 'R14h: second re-enable did not authorize';
  end if;
end $$;

rollback;

do $$ begin
  raise notice 'ALL LIVE WHATSAPP PROBES PASSED';
end $$;
