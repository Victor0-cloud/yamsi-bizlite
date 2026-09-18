-- Adversarial SQL probes for the Telegram review-backup migration.
--
-- NOT a migration: this file is never applied by `supabase db reset`.
-- Run it explicitly against the LOCAL database after a reset:
--
--   supabase db reset --local
--   supabase db execute --local -f supabase/probes/telegram_adversarial_probes.sql
--
-- Every probe is self-validating: any failed assertion raises, aborting the
-- run. A clean run ends with the 'ALL TELEGRAM PROBES PASSED' notice.
-- Probes use fixed fixture ids and precomputed sha256 token hashes (the
-- plaintext probe tokens never touch the database).

begin;

-- ---------------------------------------------------------------------------
-- Fixtures: two tenants, reporter + reviewer + outsider employees.
-- ---------------------------------------------------------------------------
insert into public.biz_tenants (id, name) values
  ('00000000-0000-0000-0000-00000000aa01', 'probe-tenant-1'),
  ('00000000-0000-0000-0000-00000000aa02', 'probe-tenant-2');

insert into public.biz_businesses (tenant_id, id, name, business_type) values
  ('00000000-0000-0000-0000-00000000aa01', 'probe-shop', 'Probe Shop', 'retail_store'),
  ('00000000-0000-0000-0000-00000000aa02', 'probe-shop', 'Probe Shop 2', 'retail_store');

insert into public.biz_branches (tenant_id, business_id, id, name) values
  ('00000000-0000-0000-0000-00000000aa01', 'probe-shop', 'main', 'Main'),
  ('00000000-0000-0000-0000-00000000aa02', 'probe-shop', 'main', 'Main');

insert into public.biz_employees (tenant_id, id, display_name, active) values
  ('00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb01', 'Probe Reporter', true),
  ('00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02', 'Probe Reviewer', true),
  ('00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb03', 'Probe Departed', false),
  ('00000000-0000-0000-0000-00000000aa02', '00000000-0000-0000-0000-00000000bb04', 'Probe Outsider', true);

-- Reviewer identity rows (numeric chat ids only -- never names).
insert into public.biz_sender_identities (tenant_id, provider, provider_sender, employee_id) values
  ('00000000-0000-0000-0000-00000000aa01', 'telegram', '1001', '00000000-0000-0000-0000-00000000bb02'),
  ('00000000-0000-0000-0000-00000000aa01', 'telegram', '1002', '00000000-0000-0000-0000-00000000bb01'),
  ('00000000-0000-0000-0000-00000000aa02', 'telegram', '2001', '00000000-0000-0000-0000-00000000bb04');

-- Explicit reviewer authorization for the tenant-1 reviewer.
insert into public.biz_review_authorizations
  (tenant_id, business_id, branch_id, employee_id, can_confirm, can_reject, active) values
  ('00000000-0000-0000-0000-00000000aa01', 'probe-shop', 'main',
    '00000000-0000-0000-0000-00000000bb02', true, true, true);

-- One draft sale submission by the reporter, via a fixture inbox row.
insert into public.biz_message_inbox
  (id, provider, provider_account, provider_event_id, status, payload) values
  ('00000000-0000-0000-0000-00000000cc01', 'whatsapp', 'probe-acct',
    'probe-event-1', 'processed',
    '{"kind": "message", "event": {"from": "1002", "type": "text"}}');

insert into public.biz_submissions
  (id, tenant_id, business_id, branch_id, employee_id, inbox_id,
   idempotency_key, kind, payload, status) values
  ('00000000-0000-0000-0000-00000000dd01',
   '00000000-0000-0000-0000-00000000aa01', 'probe-shop', 'main',
   '00000000-0000-0000-0000-00000000bb01',
   '00000000-0000-0000-0000-00000000cc01',
   'probe-sub-1', 'sale',
   '{"parsed": {"fields": {"quantity": 2, "unit": "bag", "unit_price": 500}}}',
   'draft');

-- ---------------------------------------------------------------------------
-- P1. Privilege posture: RLS on with no policies, no caller-role access,
-- service_role may EXECUTE the RPCs only.
-- ---------------------------------------------------------------------------
do $$
begin
  if not (select rowsecurity from pg_catalog.pg_tables
      where schemaname = 'public' and tablename = 'biz_telegram_links') then
    raise exception 'P1: RLS is off on biz_telegram_links';
  end if;
  if not (select rowsecurity from pg_catalog.pg_tables
      where schemaname = 'public' and tablename = 'biz_telegram_callbacks') then
    raise exception 'P1: RLS is off on biz_telegram_callbacks';
  end if;
  if (select count(*) from pg_catalog.pg_policies
      where schemaname = 'public'
        and tablename in ('biz_telegram_links', 'biz_telegram_callbacks')) <> 0 then
    raise exception 'P1: unexpected RLS policy on telegram tables';
  end if;
  if has_table_privilege('anon', 'public.biz_telegram_links', 'SELECT')
      or has_table_privilege('authenticated', 'public.biz_telegram_links', 'SELECT')
      or has_table_privilege('service_role', 'public.biz_telegram_links', 'SELECT')
      or has_table_privilege('anon', 'public.biz_telegram_callbacks', 'INSERT')
      or has_table_privilege('authenticated', 'public.biz_telegram_callbacks', 'SELECT')
      or has_table_privilege('service_role', 'public.biz_telegram_callbacks', 'SELECT') then
    raise exception 'P1: caller role holds direct telegram-table access';
  end if;
  if not has_function_privilege('service_role',
      'public.amose_issue_telegram_link(uuid, uuid, text, text, timestamptz)', 'EXECUTE')
      or not has_function_privilege('service_role',
      'public.amose_consume_telegram_link(text, text)', 'EXECUTE')
      or not has_function_privilege('service_role',
      'public.amose_queue_telegram_review_requests(uuid, text, text)', 'EXECUTE')
      or not has_function_privilege('service_role',
      'public.amose_mint_telegram_callback(text, text, text, text, text, text, timestamptz)',
      'EXECUTE')
      or not has_function_privilege('service_role',
      'public.amose_consume_telegram_callback(text, text, text)', 'EXECUTE') then
    raise exception 'P1: service_role is missing a telegram RPC grant';
  end if;
  if has_function_privilege('anon', 'public.amose_consume_telegram_link(text, text)', 'EXECUTE')
      or has_function_privilege('authenticated',
        'public.amose_consume_telegram_callback(text, text, text)', 'EXECUTE') then
    raise exception 'P1: untrusted role can execute a telegram RPC';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- P2. Link lifecycle: issue -> consume -> single-use; expired and unknown
-- links share one generic NOT_FOUND.
-- ---------------------------------------------------------------------------
do $$
declare
  v_result jsonb;
begin
  -- probe-link-1 binds chat 1003 to the reviewer.
  v_result := public.amose_issue_telegram_link(
    '00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02',
    'ce7c21adfab5bebb46a322e428ab3965ff28f589d08386093df8765d16b8d0ff',
    'probe-link-key-1', now() + interval '24 hours');
  if v_result->>'status' <> 'issued' then
    raise exception 'P2: issue failed: %', v_result;
  end if;

  v_result := public.amose_consume_telegram_link(
    'ce7c21adfab5bebb46a322e428ab3965ff28f589d08386093df8765d16b8d0ff', '1003');
  if v_result->>'status' <> 'linked'
      or v_result->>'employee_id' <> '00000000-0000-0000-0000-00000000bb02' then
    raise exception 'P2: consume failed: %', v_result;
  end if;
  if not exists (select 1 from public.biz_sender_identities
      where provider = 'telegram' and provider_sender = '1003'
        and employee_id = '00000000-0000-0000-0000-00000000bb02') then
    raise exception 'P2: binding row was not created';
  end if;

  -- Replay of the same token: NOT_FOUND (single-use), binding untouched.
  begin
    perform public.amose_consume_telegram_link(
      'ce7c21adfab5bebb46a322e428ab3965ff28f589d08386093df8765d16b8d0ff', '1003');
    raise exception 'P2: replayed link was accepted';
  exception when raise_exception then
    if sqlerrm not like 'NOT_FOUND:%' then
      raise exception 'P2: replay leaked distinct state: %', sqlerrm;
    end if;
  end;

  -- Unknown token: identical generic refusal (no oracle).
  begin
    perform public.amose_consume_telegram_link(
      'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', '1003');
    raise exception 'P2: forged link was accepted';
  exception when raise_exception then
    if sqlerrm not like 'NOT_FOUND:%' then
      raise exception 'P2: forged link leaked distinct state: %', sqlerrm;
    end if;
  end;

  -- Expired link reads exactly like unknown/used. Age BOTH timestamps so
  -- the expires_at > created_at table CHECK still holds while the link is
  -- stale relative to now().
  perform public.amose_issue_telegram_link(
    '00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02',
    '6fc3870c79a94d27568280da784dd448f5453e26e36c097ee311a5b5c494038c',
    'probe-link-key-2', now() + interval '1 hour');
  update public.biz_telegram_links
    set created_at = now() - interval '2 hours',
        expires_at = now() - interval '1 second'
    where token_hash = '6fc3870c79a94d27568280da784dd448f5453e26e36c097ee311a5b5c494038c';
  begin
    perform public.amose_consume_telegram_link(
      '6fc3870c79a94d27568280da784dd448f5453e26e36c097ee311a5b5c494038c', '1004');
    raise exception 'P2: expired link was accepted';
  exception when raise_exception then
    if sqlerrm not like 'NOT_FOUND:%' then
      raise exception 'P2: expired link leaked distinct state: %', sqlerrm;
    end if;
  end;
  if exists (select 1 from public.biz_sender_identities
      where provider = 'telegram' and provider_sender = '1004') then
    raise exception 'P2: expired link created a binding';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- P3. Cross-employee binding conflict fails closed with no writes.
-- probe-link-3 targets the reviewer but presents chat 1002 (reporter's).
-- ---------------------------------------------------------------------------
do $$
begin
  perform public.amose_issue_telegram_link(
    '00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02',
    '1d6cc76ce485c81d6ff08756d671662fe061c37759c21101134c5f98a69eb04c',
    'probe-link-key-3', now() + interval '24 hours');
  begin
    perform public.amose_consume_telegram_link(
      '1d6cc76ce485c81d6ff08756d671662fe061c37759c21101134c5f98a69eb04c', '1002');
    raise exception 'P3: conflicting bind was accepted';
  exception when raise_exception then
    if sqlerrm not like 'CONFLICT:%' then
      raise exception 'P3: wrong error: %', sqlerrm;
    end if;
  end;
  -- Link stays unused; reporter's binding is untouched.
  if (select used_at from public.biz_telegram_links
      where token_hash = '1d6cc76ce485c81d6ff08756d671662fe061c37759c21101134c5f98a69eb04c')
      is not null then
    raise exception 'P3: refused link was consumed';
  end if;
  if (select employee_id from public.biz_sender_identities
      where provider = 'telegram' and provider_sender = '1002')
      <> '00000000-0000-0000-0000-00000000bb01' then
    raise exception 'P3: reporter binding was disturbed';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- P4. Untrusted identifiers are never bound: usernames / phones rejected.
-- ---------------------------------------------------------------------------
do $$
begin
  perform public.amose_issue_telegram_link(
    '00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02',
    'b578977536f55435becb424ae675e19fdec5be729217a60972837bb210cb6c9a',
    'probe-link-key-4', now() + interval '24 hours');
  begin
    perform public.amose_consume_telegram_link(
      'b578977536f55435becb424ae675e19fdec5be729217a60972837bb210cb6c9a', 'spoofed_name');
    raise exception 'P4: username sender was accepted';
  exception when raise_exception then
    if sqlerrm not like 'MALFORMED:%' then
      raise exception 'P4: wrong error: %', sqlerrm;
    end if;
  end;
  begin
    perform public.amose_consume_telegram_link(
      'b578977536f55435becb424ae675e19fdec5be729217a60972837bb210cb6c9a', '+12025550123');
    raise exception 'P4: phone sender was accepted';
  exception when raise_exception then
    if sqlerrm not like 'MALFORMED:%' then
      raise exception 'P4: wrong error: %', sqlerrm;
    end if;
  end;
end $$;

-- ---------------------------------------------------------------------------
-- P5. Telegram queue: explicit outcomes, telegram-only routing, idempotent
-- retry with a tg_ prefix that can never collide with WhatsApp keys.
-- ---------------------------------------------------------------------------
do $$
declare
  v_result jsonb;
begin
  v_result := public.amose_queue_telegram_review_requests(
    '00000000-0000-0000-0000-00000000dd01', 'probe-queue-1', 'YamsiBizLiteBot');
  if v_result->>'status' <> 'queued' then
    raise exception 'P5: queue failed: %', v_result;
  end if;
  if not exists (select 1 from public.biz_outbound_messages
      where provider = 'telegram'
        and provider_sender = '1001'
        and provider_account = 'YamsiBizLiteBot'
        and message_type = 'review_request'
        and related_task_id is null
        and idempotency_key like 'tg_review_req:%') then
    raise exception 'P5: telegram outbound row is missing or malformed';
  end if;
  if exists (select 1 from public.biz_outbound_messages
      where idempotency_key like 'tg_review_req:%'
        and provider <> 'telegram') then
    raise exception 'P5: telegram queue wrote a non-telegram row';
  end if;
  -- Reporter (1002) is skipped, never notified.
  if exists (select 1 from public.biz_outbound_messages
      where idempotency_key like 'tg_review_req:%'
        and provider_sender = '1002') then
    raise exception 'P5: reporter was notified (separation of duties)';
  end if;
  -- Idempotent retry: same key, no duplicates.
  v_result := public.amose_queue_telegram_review_requests(
    '00000000-0000-0000-0000-00000000dd01', 'probe-queue-1', 'YamsiBizLiteBot');
  if v_result->>'status' <> 'already_queued' then
    raise exception 'P5: retry was not idempotent: %', v_result;
  end if;
  if (select count(*) from public.biz_outbound_messages
      where idempotency_key like 'tg_review_req:%') <> 1 then
    raise exception 'P5: retry duplicated outbound rows';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- P6. Callbacks: mint enforces authorization; consume is single-use and
-- replay-safe with a stable key; forgeries and cross-tenant presses fail.
-- ---------------------------------------------------------------------------
do $$
declare
  v_queue jsonb;
  v_ref text;
  v_mint jsonb;
  v_first jsonb;
  v_second jsonb;
begin
  v_queue := public.amose_queue_telegram_review_requests(
    '00000000-0000-0000-0000-00000000dd01', 'probe-queue-2', 'YamsiBizLiteBot');
  v_ref := v_queue->>'review_ref';

  -- Cross-tenant press at mint time: tenant-2 sender against a tenant-1
  -- reference must fail (reads as NOT_FOUND; never UNAUTHORIZED-oracle).
  begin
    perform public.amose_mint_telegram_callback(
      '8c25f776df38e5da9f0a9b76309d34210846a0891bd240ba32e52188fca9fe71',
      v_ref, 'approve', 'telegram', '2001', 'probe-cb-key-x',
      now() + interval '72 hours');
    raise exception 'P6: cross-tenant mint was accepted';
  exception when raise_exception then
    if sqlerrm not like 'NOT_FOUND:%' and sqlerrm not like 'UNAUTHORIZED:%' then
      raise exception 'P6: wrong error: %', sqlerrm;
    end if;
  end;

  -- Legitimate mint for the bound reviewer.
  v_mint := public.amose_mint_telegram_callback(
    '8c25f776df38e5da9f0a9b76309d34210846a0891bd240ba32e52188fca9fe71',
    v_ref, 'approve', 'telegram', '1001', 'probe-cb-key-1',
    now() + interval '72 hours');
  if v_mint->>'status' <> 'minted' then
    raise exception 'P6: mint failed: %', v_mint;
  end if;

  -- First press: ready. Replay: same parameters, already_used.
  v_first := public.amose_consume_telegram_callback(
    '8c25f776df38e5da9f0a9b76309d34210846a0891bd240ba32e52188fca9fe71',
    'telegram', '1001');
  if v_first->>'status' <> 'ready' then
    raise exception 'P6: first press failed: %', v_first;
  end if;
  v_second := public.amose_consume_telegram_callback(
    '8c25f776df38e5da9f0a9b76309d34210846a0891bd240ba32e52188fca9fe71',
    'telegram', '1001');
  if v_second->>'status' <> 'already_used'
      or v_second->>'request_key' <> v_first->>'request_key'
      or v_second->>'review_ref' <> v_first->>'review_ref' then
    raise exception 'P6: replay was not stable: %', v_second;
  end if;

  -- Forged token: NOT_FOUND, nothing consumed.
  begin
    perform public.amose_consume_telegram_callback(
      '0000000000000000000000000000000000000000000000000000000000000000',
      'telegram', '1001');
    raise exception 'P6: forged callback was accepted';
  exception when raise_exception then
    if sqlerrm not like 'NOT_FOUND:%' then
      raise exception 'P6: forged callback leaked state: %', sqlerrm;
    end if;
  end;

  -- Wrong reviewer pressing a live token: UNAUTHORIZED.
  perform public.amose_mint_telegram_callback(
    '8ef2e7543a1d261c37a0f4cd193daf0d8be87f49de06d10d82ad0629c679b457',
    v_ref, 'reject', 'telegram', '1001', 'probe-cb-key-2',
    now() + interval '72 hours');
  begin
    perform public.amose_consume_telegram_callback(
      '8ef2e7543a1d261c37a0f4cd193daf0d8be87f49de06d10d82ad0629c679b457',
      'telegram', '1002');
    raise exception 'P6: wrong-reviewer press was accepted';
  exception when raise_exception then
    if sqlerrm not like 'UNAUTHORIZED:%' then
      raise exception 'P6: wrong error: %', sqlerrm;
    end if;
  end;
end $$;

-- ---------------------------------------------------------------------------
-- P7. Decided cases retire buttons: reject the draft through the existing
-- Phase 3 RPC, then minting fails and pressing reports case_closed.
-- ---------------------------------------------------------------------------
do $$
declare
  v_queue jsonb;
  v_ref text;
  v_press jsonb;
begin
  v_queue := public.amose_queue_telegram_review_requests(
    '00000000-0000-0000-0000-00000000dd01', 'probe-queue-3', 'YamsiBizLiteBot');
  v_ref := v_queue->>'review_ref';

  perform public.amose_mint_telegram_callback(
    '142cd93273a8a24282684236ed1ad6549e2bea0810ae9a35fcd53ac2e3311f1e',
    v_ref, 'approve', 'telegram', '1001', 'probe-cb-key-3',
    now() + interval '72 hours');

  -- Decide through the EXISTING review boundary (never the adapter).
  perform public.amose_reject_submission(
    v_ref, 'telegram', '1001', 'probe rejection reason', 'probe-reject-1');

  v_press := public.amose_consume_telegram_callback(
    '142cd93273a8a24282684236ed1ad6549e2bea0810ae9a35fcd53ac2e3311f1e',
    'telegram', '1001');
  if v_press->>'status' <> 'case_closed' then
    raise exception 'P7: decided press misreported: %', v_press;
  end if;

  begin
    perform public.amose_mint_telegram_callback(
      'df806c999eb27b259642decebd81459ec63a330ac9bf49f1a1038bfa6bf65c33',
      v_ref, 'approve', 'telegram', '1001', 'probe-cb-key-4',
      now() + interval '72 hours');
    raise exception 'P7: mint on a decided case was accepted';
  exception when raise_exception then
    if sqlerrm not like 'CONFLICT:%' then
      raise exception 'P7: wrong error: %', sqlerrm;
    end if;
  end;
end $$;

-- ---------------------------------------------------------------------------
-- P8. Rollback: a refused queue call writes nothing (no case, no rows).
-- P9. Issue idempotency: same (tenant, key) never rotates the token.
-- ---------------------------------------------------------------------------
do $$
declare
  v_cases int;
  v_first jsonb;
  v_second jsonb;
begin
  select count(*) into v_cases from public.biz_review_cases;
  begin
    perform public.amose_queue_telegram_review_requests(
      '00000000-0000-0000-0000-00000000dd01', '', 'YamsiBizLiteBot');
    raise exception 'P8: malformed queue call was accepted';
  exception when raise_exception then
    if sqlerrm not like 'MALFORMED:%' then
      raise exception 'P8: wrong error: %', sqlerrm;
    end if;
  end;
  if (select count(*) from public.biz_review_cases) <> v_cases then
    raise exception 'P8: refused queue call left a case row';
  end if;

  v_first := public.amose_issue_telegram_link(
    '00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02',
    'ce7c21adfab5bebb46a322e428ab3965ff28f589d08386093df8765d16b8d0ff',
    'probe-link-key-1', now() + interval '24 hours');
  if (v_first->>'is_retry')::boolean is not true then
    raise exception 'P8: repeat issue was not a retry: %', v_first;
  end if;
  begin
    perform public.amose_issue_telegram_link(
      '00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-00000000bb02',
      '6fc3870c79a94d27568280da784dd448f5453e26e36c097ee311a5b5c494038c',
      'probe-link-key-1', now() + interval '24 hours');
    raise exception 'P9: rotated token on the same key was accepted';
  exception when raise_exception then
    if sqlerrm not like 'CONFLICT:%' then
      raise exception 'P9: wrong error: %', sqlerrm;
    end if;
  end;
end $$;

rollback;

do $$ begin
  raise notice 'ALL TELEGRAM PROBES PASSED';
end $$;
