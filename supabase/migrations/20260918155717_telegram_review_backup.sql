-- Telegram review-backup channel adapter.
--
-- Schema + code only: no business data, no authorization rows, and no
-- secrets are seeded. All database changes for this phase live in THIS
-- migration; older migrations are untouched and WhatsApp behavior is
-- unchanged.
--
-- Telegram is a channel adapter, not a second business workflow: every
-- approval, rejection, or correction executes through the existing Phase 3
-- RPCs (amose_confirm_submission / amose_reject_submission) and the Phase 4
-- chat-confirm RPC (amose_review_confirm_command). This migration adds NO
-- posting logic and performs NO direct writes to submissions, operational,
-- Brain, audit, or review-decision tables outside those existing RPCs.
--
-- What this migration adds:
--   1. public.biz_telegram_links: one-time, expiring account-link tokens.
--      Only the sha256 hex of each token is stored -- the plaintext token
--      is shown once to the trusted issuer (owner tooling) and never again.
--      Telegram display names, usernames, phone numbers, chat IDs, and
--      callback data are NEVER trusted: linking binds a numeric Telegram
--      chat/sender id to an employee only through consuming a valid link.
--   2. public.amose_issue_telegram_link(...): trusted administrative
--      boundary (service_role EXECUTE only). Idempotent per
--      (tenant, request_key): repeats return the stored row without
--      rotating the token.
--   3. public.amose_consume_telegram_link(...): atomically validates a
--      link (unknown / expired / already-used all read as one generic
--      NOT_FOUND so tokens cannot be probed), then binds
--      (provider='telegram', provider_sender=<numeric chat id>) to the
--      link's employee in biz_sender_identities. Single-use: the link is
--      marked consumed inside the same transaction. A chat id already
--      bound to a DIFFERENT employee fails closed with CONFLICT.
--   4. public.amose_queue_telegram_review_requests(...): mirrors
--      amose_queue_review_requests for provider='telegram' (eligible
--      reviewers with a verified Telegram identity, reporter skipped,
--      per-(case, reviewer) idempotency keys with a 'tg_review_req:'
--      prefix). Builds the same safe message text through the existing
--      _amose_review_request_text helper -- never UUIDs, secrets, or raw
--      payloads. Outcomes are explicit: queued / already_queued /
--      no_eligible_reviewer / reviewer_unroutable. The draft stays draft.
--   5. public.biz_telegram_callbacks: short opaque button tokens for
--      inline-keyboard callbacks (Telegram caps callback_data at 64
--      bytes, so the token is random and every parameter resolves
--      server-side). Only the sha256 hex is stored. Minting replaces any
--      prior token for the same (case, action, reviewer), so superseded
--      buttons fail closed.
--   6. public.amose_mint_telegram_callback(...): validates reviewer
--      identity, open case, and explicit authorization (confirm or reject
--      capability plus separation of duties) before storing a token.
--   7. public.amose_consume_telegram_callback(...): transactionally
--      validates token binding, expiry, and case state. First use returns
--      'ready'; a repeat returns the SAME stored parameters with
--      'already_used' so the caller retries the SAME idempotent review
--      RPC (crash-safe, replay-safe: the review request key is stable per
--      token). Decided or expired tokens return 'case_closed' / 'expired'
--      with no review call. Unknown tokens read as NOT_FOUND.
--
-- Security model: RLS enabled on both new tables with no policies (fail
-- closed); ALL revoked from PUBLIC/anon/authenticated/service_role (no
-- direct access for any caller role). New RPCs are SECURITY DEFINER with
-- a fixed search_path (pg_catalog) and schema-qualified references,
-- EXECUTE revoked from PUBLIC/anon/authenticated and granted to
-- service_role only.
begin;

-- ---------------------------------------------------------------------------
-- 1. One-time account-link tokens. Plaintext tokens never touch this table.
-- ---------------------------------------------------------------------------
create table public.biz_telegram_links (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  employee_id uuid not null,
  token_hash text not null unique,
  request_key text not null,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, employee_id)
    references public.biz_employees(tenant_id, id),
  check (token_hash ~ '^[0-9a-f]{64}$'),
  check (pg_catalog.char_length(request_key) between 1 and 128),
  check (expires_at > created_at),
  check (used_at is null or used_at >= created_at),
  unique (tenant_id, request_key)
);
comment on table public.biz_telegram_links is
  'One-time Telegram account-link tokens (sha256 hex only). Single-use, expiring; consumed by amose_consume_telegram_link.';
create index biz_telegram_links_employee
  on public.biz_telegram_links(tenant_id, employee_id);

-- ---------------------------------------------------------------------------
-- 2. Inline-button callback tokens. Random per mint; parameters resolve
-- server-side so forged or tampered button presses fail closed.
-- ---------------------------------------------------------------------------
create table public.biz_telegram_callbacks (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  tenant_id uuid not null references public.biz_tenants(id),
  business_id text not null,
  branch_id text not null,
  submission_id uuid not null,
  review_ref text not null,
  action text not null check (action in ('approve', 'reject')),
  reviewer_employee_id uuid not null,
  token_hash text not null unique,
  request_key text not null,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default pg_catalog.now(),
  foreign key (tenant_id, business_id)
    references public.biz_businesses(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id)
    references public.biz_branches(tenant_id, business_id, id),
  foreign key (tenant_id, reviewer_employee_id)
    references public.biz_employees(tenant_id, id),
  foreign key (tenant_id, business_id, branch_id, submission_id)
    references public.biz_submissions(tenant_id, business_id, branch_id, id),
  check (token_hash ~ '^[0-9a-f]{64}$'),
  check (pg_catalog.char_length(request_key) between 1 and 128),
  check (expires_at > created_at),
  check (used_at is null or used_at >= created_at),
  -- One live button per (case, action, reviewer): minting replaces the
  -- prior token for the same triple, retiring superseded buttons.
  unique (tenant_id, review_ref, action, reviewer_employee_id),
  unique (tenant_id, request_key)
);
comment on table public.biz_telegram_callbacks is
  'Opaque Telegram inline-button tokens (sha256 hex only). Consumed by amose_consume_telegram_callback; review itself runs in the Phase 3/4 RPCs.';
create index biz_telegram_callbacks_case
  on public.biz_telegram_callbacks(tenant_id, review_ref);

-- ---------------------------------------------------------------------------
-- 3. Issue a one-time link (trusted administrative boundary only).
-- ---------------------------------------------------------------------------
create or replace function public.amose_issue_telegram_link(
  p_tenant_id uuid,
  p_employee_id uuid,
  p_token_hash text,
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
  v_key text;
  v_row public.biz_telegram_links%rowtype;
begin
  if p_tenant_id is null or p_employee_id is null then
    raise exception 'NOT_FOUND: employee is not known';
  end if;
  v_hash := nullif(btrim(p_token_hash), '');
  if v_hash is null or v_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'MALFORMED: token hash must be sha256 hex';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  if p_expires_at is null or p_expires_at <= pg_catalog.now()
      or p_expires_at > pg_catalog.now() + interval '30 days' then
    raise exception 'MALFORMED: link expiry must be within the next 30 days';
  end if;

  -- The employee must exist AND be active in the tenant; anything else
  -- fails closed so a link can never be issued for a stranger.
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = p_tenant_id and e.id = p_employee_id
        and e.active = true) then
    raise exception 'NOT_FOUND: employee is not known';
  end if;

  -- Idempotent per (tenant, request key): repeats return the stored row
  -- WITHOUT rotating the token (the original token was already handed to
  -- the owner for delivery).
  select * into v_row from public.biz_telegram_links l
    where l.tenant_id = p_tenant_id and l.request_key = v_key;
  if found then
    if v_row.token_hash is distinct from v_hash then
      raise exception 'CONFLICT: request key was reused with different data';
    end if;
    return jsonb_build_object('status', 'issued',
      'link_id', v_row.id::text,
      'tenant_id', v_row.tenant_id::text,
      'employee_id', v_row.employee_id::text,
      'request_key', v_row.request_key,
      'expires_at', v_row.expires_at::text,
      'is_retry', true);
  end if;

  insert into public.biz_telegram_links
    (tenant_id, employee_id, token_hash, request_key, expires_at)
    values (p_tenant_id, p_employee_id, v_hash, v_key, p_expires_at)
    returning * into v_row;
  return jsonb_build_object('status', 'issued',
    'link_id', v_row.id::text,
    'tenant_id', v_row.tenant_id::text,
    'employee_id', v_row.employee_id::text,
    'request_key', v_row.request_key,
    'expires_at', v_row.expires_at::text,
    'is_retry', false);
exception when unique_violation then
  -- A concurrent insert won the race: an identical row is an idempotent
  -- retry, anything else is a genuine key conflict.
  select * into v_row from public.biz_telegram_links l
    where l.tenant_id = p_tenant_id and l.request_key = v_key;
  if found and v_row.token_hash = v_hash
      and v_row.employee_id = p_employee_id then
    return jsonb_build_object('status', 'issued',
      'link_id', v_row.id::text,
      'tenant_id', v_row.tenant_id::text,
      'employee_id', v_row.employee_id::text,
      'request_key', v_row.request_key,
      'expires_at', v_row.expires_at::text,
      'is_retry', true);
  end if;
  raise exception 'CONFLICT: request key was reused with different data';
end;
$func$;

-- ---------------------------------------------------------------------------
-- 4. Consume a one-time link: bind a numeric Telegram sender id to the
-- link's employee. Unknown / expired / already-used links share one
-- generic NOT_FOUND message so tokens cannot be probed.
-- ---------------------------------------------------------------------------
create or replace function public.amose_consume_telegram_link(
  p_token_hash text,
  p_provider_sender text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_hash text;
  v_sender text;
  v_link public.biz_telegram_links%rowtype;
  v_existing public.biz_sender_identities%rowtype;
begin
  v_hash := nullif(btrim(p_token_hash), '');
  if v_hash is null or v_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'NOT_FOUND: link is not valid';
  end if;
  -- Only a numeric Telegram sender/chat id is ever bound. Display names,
  -- usernames, and phone numbers are never accepted here.
  v_sender := nullif(btrim(p_provider_sender), '');
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;

  select * into v_link from public.biz_telegram_links l
    where l.token_hash = v_hash
    for update;
  if not found or v_link.expires_at <= pg_catalog.now()
      or v_link.used_at is not null then
    raise exception 'NOT_FOUND: link is not valid';
  end if;

  -- The grantee must still be an active employee; a departed employee's
  -- link dies here, fail closed.
  if not exists (select 1 from public.biz_employees e
      where e.tenant_id = v_link.tenant_id and e.id = v_link.employee_id
        and e.active = true) then
    raise exception 'UNAUTHORIZED: link cannot be consumed';
  end if;

  select * into v_existing from public.biz_sender_identities i
    where i.provider = 'telegram' and i.provider_sender = v_sender;
  if found then
    if v_existing.tenant_id is distinct from v_link.tenant_id
        or v_existing.employee_id is distinct from v_link.employee_id then
      -- This chat id already belongs to someone else: refuse without
      -- touching either row.
      raise exception 'CONFLICT: sender is already linked to a different employee';
    end if;
    -- Same binding re-presented (should not normally happen for a
    -- single-use link, but harmless): consume the link, keep the row.
  else
    begin
      insert into public.biz_sender_identities
        (tenant_id, provider, provider_sender, employee_id)
        values (v_link.tenant_id, 'telegram', v_sender, v_link.employee_id);
    exception when unique_violation then
      select * into v_existing from public.biz_sender_identities i
        where i.provider = 'telegram' and i.provider_sender = v_sender;
      if not found or v_existing.tenant_id is distinct from v_link.tenant_id
          or v_existing.employee_id is distinct from v_link.employee_id then
        raise exception 'CONFLICT: sender is already linked to a different employee';
      end if;
    end;
  end if;

  update public.biz_telegram_links l
    set used_at = pg_catalog.now()
    where l.id = v_link.id;

  return jsonb_build_object('status', 'linked',
    'tenant_id', v_link.tenant_id::text,
    'employee_id', v_link.employee_id::text,
    'provider', 'telegram',
    'provider_sender', v_sender);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 5. Queue Telegram review requests for a draft submission. Telegram twin
-- of amose_queue_review_requests: same reference issuance, same outcome
-- discipline, per-(case, reviewer) idempotency with a 'tg_review_req:'
-- prefix so Telegram retries can never collide with WhatsApp rows.
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
      'payment', 'expense') then
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, which needs no human review',
      v_sub, v_kind;
  end if;
  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;

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

  v_msg := public._amose_review_request_text(
    v_kind, v_business, v_branch, v_parsed, v_ref);

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

-- ---------------------------------------------------------------------------
-- 6. Mint one inline-button token. Authorization (capability + separation
-- of duties) is enforced here AND again inside the Phase 3/4 review RPCs
-- at press time. Minting replaces any prior token for the same
-- (case, action, reviewer), retiring superseded buttons.
-- ---------------------------------------------------------------------------
create or replace function public.amose_mint_telegram_callback(
  p_token_hash text,
  p_review_ref text,
  p_action text,
  p_reviewer_provider text,
  p_reviewer_sender text,
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
  v_ref text;
  v_key text;
  v_sender text;
  v_tenant uuid; v_reviewer uuid;
  v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_status text;
  v_need text;
begin
  v_hash := nullif(btrim(p_token_hash), '');
  if v_hash is null or v_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'MALFORMED: token hash must be sha256 hex';
  end if;
  v_ref := nullif(btrim(p_review_ref), '');
  if v_ref is null or v_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  if p_action is null or p_action not in ('approve', 'reject') then
    raise exception 'MALFORMED: callback action must be approve or reject';
  end if;
  if p_reviewer_provider is null or p_reviewer_provider <> 'telegram' then
    raise exception 'MALFORMED: callback provider must be telegram';
  end if;
  v_sender := nullif(btrim(p_reviewer_sender), '');
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  if p_expires_at is null or p_expires_at <= pg_catalog.now()
      or p_expires_at > pg_catalog.now() + interval '30 days' then
    raise exception 'MALFORMED: callback expiry must be within the next 30 days';
  end if;

  -- Verified reviewer identity: tenant comes from this trusted row, never
  -- from the caller.
  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, v_sender) as o;

  -- Lock the case inside the reviewer's own tenant; cross-tenant guesses
  -- read as NOT_FOUND.
  select c.business_id, c.branch_id, c.submission_id, s.employee_id, c.status
    into v_business, v_branch, v_sub, v_submitter, v_status
    from public.biz_review_cases c
    join public.biz_submissions s
      on s.tenant_id = c.tenant_id
      and s.business_id = c.business_id
      and s.branch_id = c.branch_id
      and s.id = c.submission_id
    where c.tenant_id = v_tenant
      and c.review_ref = v_ref
    for update of c;
  if not found then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  if v_status is distinct from 'open' then
    raise exception 'CONFLICT: review case is already decided';
  end if;

  -- Explicit authorization plus separation of duties, enforced now and
  -- again at press time inside the review RPCs.
  v_need := case when p_action = 'approve' then 'confirm' else 'reject' end;
  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_business, v_branch, v_submitter, v_need);

  -- Retire any superseded button for the same (case, action, reviewer).
  delete from public.biz_telegram_callbacks cb
    where cb.tenant_id = v_tenant
      and cb.review_ref = v_ref
      and cb.action = p_action
      and cb.reviewer_employee_id = v_reviewer;

  begin
    insert into public.biz_telegram_callbacks
      (tenant_id, business_id, branch_id, submission_id, review_ref,
       action, reviewer_employee_id, token_hash, request_key, expires_at)
      values (v_tenant, v_business, v_branch, v_sub, v_ref,
        p_action, v_reviewer, v_hash, v_key, p_expires_at);
  exception when unique_violation then
    raise exception 'CONFLICT: callback could not be minted';
  end;

  return jsonb_build_object('status', 'minted',
    'tenant_id', v_tenant::text,
    'business_id', v_business,
    'branch_id', v_branch,
    'submission_id', v_sub::text,
    'review_ref', v_ref,
    'action', p_action,
    'reviewer_employee_id', v_reviewer::text,
    'request_key', v_key,
    'expires_at', p_expires_at::text);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 7. Consume one inline-button token. First press returns 'ready'; a repeat
-- returns the SAME stored parameters with 'already_used' so the caller
-- retries the SAME idempotent review RPC (stable request key) instead of
-- minting a second decision. Decided/expired presses retire the token and
-- report state with no review call. Unknown tokens read as NOT_FOUND and
-- binding mismatches (including cross-tenant guesses) as UNAUTHORIZED, so
-- forged presses reveal nothing and change nothing.
-- ---------------------------------------------------------------------------
create or replace function public.amose_consume_telegram_callback(
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
  v_cb public.biz_telegram_callbacks%rowtype;
  v_tenant uuid; v_reviewer uuid;
  v_case_status text;
  v_is_retry boolean;
begin
  v_hash := nullif(btrim(p_token_hash), '');
  if v_hash is null or v_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'NOT_FOUND: callback is not known';
  end if;
  if p_reviewer_provider is null or p_reviewer_provider <> 'telegram' then
    raise exception 'MALFORMED: callback provider must be telegram';
  end if;
  v_sender := nullif(btrim(p_reviewer_sender), '');
  if v_sender is null or v_sender !~ '^[0-9]{1,20}$' then
    raise exception 'MALFORMED: sender identity is not valid';
  end if;

  select * into v_cb from public.biz_telegram_callbacks cb
    where cb.token_hash = v_hash
    for update;
  if not found then
    raise exception 'NOT_FOUND: callback is not known';
  end if;

  -- The presser must be the bound reviewer in the token's own tenant.
  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, v_sender) as o;
  if v_tenant is distinct from v_cb.tenant_id
      or v_reviewer is distinct from v_cb.reviewer_employee_id then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;

  select c.status into v_case_status
    from public.biz_review_cases c
    where c.tenant_id = v_cb.tenant_id
      and c.review_ref = v_cb.review_ref;
  if not found or v_case_status is distinct from 'open' then
    if v_cb.used_at is null then
      update public.biz_telegram_callbacks cb
        set used_at = pg_catalog.now()
        where cb.id = v_cb.id;
    end if;
    return jsonb_build_object('status', 'case_closed',
      'review_ref', v_cb.review_ref,
      'action', v_cb.action,
      'request_key', v_cb.request_key,
      'tenant_id', v_cb.tenant_id::text,
      'is_retry', (v_cb.used_at is not null));
  end if;

  if v_cb.expires_at <= pg_catalog.now() then
    if v_cb.used_at is null then
      update public.biz_telegram_callbacks cb
        set used_at = pg_catalog.now()
        where cb.id = v_cb.id;
    end if;
    return jsonb_build_object('status', 'expired',
      'review_ref', v_cb.review_ref,
      'action', v_cb.action,
      'request_key', v_cb.request_key,
      'tenant_id', v_cb.tenant_id::text,
      'is_retry', (v_cb.used_at is not null));
  end if;

  v_is_retry := (v_cb.used_at is not null);
  if not v_is_retry then
    update public.biz_telegram_callbacks cb
      set used_at = pg_catalog.now()
      where cb.id = v_cb.id;
  end if;

  if v_is_retry then
    return jsonb_build_object('status', 'already_used',
      'review_ref', v_cb.review_ref,
      'action', v_cb.action,
      'request_key', v_cb.request_key,
      'tenant_id', v_cb.tenant_id::text,
      'business_id', v_cb.business_id,
      'branch_id', v_cb.branch_id,
      'submission_id', v_cb.submission_id::text,
      'reviewer_employee_id', v_cb.reviewer_employee_id::text,
      'is_retry', true);
  end if;
  return jsonb_build_object('status', 'ready',
    'review_ref', v_cb.review_ref,
    'action', v_cb.action,
    'request_key', v_cb.request_key,
    'tenant_id', v_cb.tenant_id::text,
    'business_id', v_cb.business_id,
    'branch_id', v_cb.branch_id,
    'submission_id', v_cb.submission_id::text,
    'reviewer_employee_id', v_cb.reviewer_employee_id::text,
    'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 8. RLS + least privilege. Fail closed for every caller role.
--
-- Privilege matrix (this migration; earlier posture unchanged):
--   biz_telegram_links / biz_telegram_callbacks: RLS ON, no policies;
--     PUBLIC/anon/authenticated/service_role: NO privileges (every read
--     and write runs inside the SECURITY DEFINER RPCs above).
--   biz_sender_identities / biz_message_inbox / biz_submissions /
--     biz_outbound_messages / biz_review_cases: unchanged (existing
--     service_role grants already cover the adapter's REST use).
--   Internal helpers: none added (existing Phase 3/4 helpers reused).
--   New RPCs (amose_issue_telegram_link, amose_consume_telegram_link,
--     amose_queue_telegram_review_requests,
--     amose_mint_telegram_callback, amose_consume_telegram_callback):
--     EXECUTE revoked from PUBLIC/anon/authenticated; granted to
--     service_role only.
-- ---------------------------------------------------------------------------
alter table public.biz_telegram_links enable row level security;
alter table public.biz_telegram_callbacks enable row level security;

revoke all on public.biz_telegram_links
  from public, anon, authenticated, service_role;
revoke all on public.biz_telegram_callbacks
  from public, anon, authenticated, service_role;

revoke execute on function public.amose_issue_telegram_link(uuid, uuid, text, text, timestamptz)
  from public, anon, authenticated;
revoke execute on function public.amose_consume_telegram_link(text, text)
  from public, anon, authenticated;
revoke execute on function public.amose_queue_telegram_review_requests(uuid, text, text)
  from public, anon, authenticated;
revoke execute on function public.amose_mint_telegram_callback(text, text, text, text, text, text, timestamptz)
  from public, anon, authenticated;
revoke execute on function public.amose_consume_telegram_callback(text, text, text)
  from public, anon, authenticated;

grant execute on function public.amose_issue_telegram_link(uuid, uuid, text, text, timestamptz)
  to service_role;
grant execute on function public.amose_consume_telegram_link(text, text)
  to service_role;
grant execute on function public.amose_queue_telegram_review_requests(uuid, text, text)
  to service_role;
grant execute on function public.amose_mint_telegram_callback(text, text, text, text, text, text, timestamptz)
  to service_role;
grant execute on function public.amose_consume_telegram_callback(text, text, text)
  to service_role;

commit;
