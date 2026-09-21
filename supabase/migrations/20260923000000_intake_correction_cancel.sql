-- Intake correction values and submitter cancellation.
--
-- Forward-only migration. Schema + code only: no data rows are seeded.
--
-- What changes and why:
--   1. Real CORRECTION values: REVIEW CONFIRM ... CORRECTION k=v pairs
--      now change draft values before posting instead of only recording
--      a reason. amose_review_confirm_command gains an optional
--      p_corrections jsonb argument (default null, so existing 5-argument
--      callers keep working). A new owner-only helper
--      _amose_apply_intake_corrections() allowlists one small set of
--      textual overrides per kind (amount, quantity, customer,
--      method, ...), coerces values to text, and fails closed on
--      unknown keys, nulls, or overlong values. The chat-confirm
--      builders then cast exactly as they do for parsed text, so a bad
--      correction fails with the same MALFORMED message as a bad
--      report. Corrections require a reason (the audit invariant that
--      corrections explain themselves is unchanged) and the action is
--      recorded as 'corrected' with the corrected verified snapshot.
--   2. Submitter CANCEL: the linked reporter can withdraw their own
--      pending draft with CANCEL <ref> [reason]. New service-role-only
--      RPC amose_cancel_submission() resolves the requester from the
--      numeric sender identity, requires requester = reporter (anyone
--      else, including reviewers and other tenants, gets UNAUTHORIZED),
--      requires a draft (terminal rows fail closed), and is idempotent
--      per tenant-scoped request key. Cancellation writes status
--      'cancelled' on the submission and its case plus an append-only
--      'cancelled' audit row and queues a review_cancelled ack to the
--      reporter -- fully distinct from reviewer rejection (status
--      'rejected', reviewer actor, reason required).
--   3. Cross-channel review requests: amose_queue_review_requests() no
--      longer reuses a foreign-channel inbox snapshot for WhatsApp
--      review requests. WhatsApp drafts keep the exact existing routing;
--      drafts that arrived on another channel resolve the scope's own
--      single enabled WhatsApp account (zero or several configured
--      accounts report reviewer_unroutable instead of failing).
--   4. Status CHECKs grow the 'cancelled' value on biz_submissions,
--      biz_review_cases (with the decided_at rule), and biz_review_audit
--      (cancelled rows carry no verified snapshot or posting type, with
--      an optional reason), plus the 'review_cancelled' outbound
--      message type. Existing values and rules are unchanged.
--
-- Least privilege preserved: the new RPC is revoked from
-- PUBLIC/anon/authenticated and granted to service_role only; the new
-- helper stays owner-only. CREATE OR REPLACE preserves existing grants
-- on replaced functions. RLS, sender-resolution, review-reference, and
-- idempotency rules are untouched.
begin;

-- ---------------------------------------------------------------------------
-- 1. 'cancelled' joins the status state machines. Existing values and
-- rules are unchanged; cancelled rows are terminal like rejected rows
-- (every draft-only gate already excludes them).
-- ---------------------------------------------------------------------------
alter table public.biz_submissions
  drop constraint if exists biz_submissions_status_check;
alter table public.biz_submissions
  add constraint biz_submissions_status_check
  check (status in ('draft', 'confirmed', 'rejected', 'cancelled'));

alter table public.biz_review_cases
  drop constraint if exists biz_review_cases_status_check;
alter table public.biz_review_cases
  add constraint biz_review_cases_status_check
  check (status in ('open', 'confirmed', 'rejected', 'cancelled'));

alter table public.biz_review_cases
  drop constraint if exists biz_review_cases_check;
alter table public.biz_review_cases
  add constraint biz_review_cases_check
  check (((status = 'open' and decided_at is null)
    or (status in ('confirmed', 'rejected', 'cancelled')
      and decided_at is not null)));

alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_action_check;
alter table public.biz_review_audit
  add constraint biz_review_audit_action_check
  check (action in ('corrected', 'confirmed', 'rejected', 'cancelled'));

alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_check;
alter table public.biz_review_audit
  add constraint biz_review_audit_check
  check (((action = 'rejected'
      and reason is not null and btrim(reason) <> '')
    or (action = 'corrected'
      and reason is not null and btrim(reason) <> '')
    or (action = 'confirmed' and reason is null)
    or (action = 'cancelled'
      and (reason is null or btrim(reason) <> ''))));

alter table public.biz_review_audit
  drop constraint if exists biz_review_audit_check1;
alter table public.biz_review_audit
  add constraint biz_review_audit_check1
  check (((action in ('corrected', 'confirmed')
      and verified_snapshot is not null
      and jsonb_typeof(verified_snapshot) = 'object'
      and posting_type is not null and result is not null)
    or (action = 'rejected'
      and verified_snapshot is null
      and posting_type is null and result is not null)
    or (action = 'cancelled'
      and verified_snapshot is null
      and posting_type is null and result is not null)));

alter table public.biz_outbound_messages
  drop constraint if exists biz_outbound_messages_review_task_check;
alter table public.biz_outbound_messages
  add constraint biz_outbound_messages_review_task_check
  check ((message_type in ('review_confirmed', 'review_rejected',
      'review_request', 'branch_clarification', 'review_cancelled'))
    = (related_task_id is null));

-- ---------------------------------------------------------------------------
-- 2. Corrections helper: allowlisted textual overrides per kind.
-- Owner-only; never granted to any caller role.
-- ---------------------------------------------------------------------------
create or replace function public._amose_apply_intake_corrections(
  p_kind text,
  p_fields jsonb,
  p_corrections jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_allowed text[];
  v_key text;
  v_value jsonb;
  v_text text;
  v_out jsonb := '{}'::jsonb;
begin
  if p_corrections is null then
    return '{}'::jsonb;
  end if;
  if jsonb_typeof(p_corrections) <> 'object' then
    raise exception 'MALFORMED: corrections must be field=value pairs';
  end if;
  if p_kind in ('sale') then
    v_allowed := array['quantity', 'unit_price', 'unit'];
  elsif p_kind in ('production', 'poultry_daily_report') then
    v_allowed := array['good_quantity', 'rejected_quantity', 'shift',
      'production_date'];
  elsif p_kind = 'payment' then
    v_allowed := array['amount_kobo', 'method'];
  elsif p_kind = 'expense' then
    v_allowed := array['amount_kobo', 'payment_method', 'category',
      'description'];
  elsif p_kind = 'cash_handover' then
    v_allowed := array['amount_kobo', 'from_name', 'to_name'];
  elsif p_kind = 'bank_deposit' then
    v_allowed := array['amount_kobo', 'destination_account', 'reference',
      'depositor_name'];
  elsif p_kind = 'stock' then
    v_allowed := array['normal_quantity', 'cold_quantity'];
  elsif p_kind = 'customer_payment' then
    v_allowed := array['amount_kobo', 'method', 'customer_name'];
  elsif p_kind = 'customer_debt' then
    v_allowed := array['amount_kobo', 'customer_name'];
  else
    raise exception 'UNSUPPORTED_KIND: submission kind % cannot take corrections', p_kind;
  end if;
  for v_key, v_value in select * from jsonb_each(p_corrections) loop
    if not (v_key = any (v_allowed)) then
      raise exception 'MALFORMED: correction field % is not allowed for this report; allowed: %',
        left(v_key, 40), array_to_string(v_allowed, ', ');
    end if;
    if jsonb_typeof(v_value) = 'string' then
      v_text := btrim(v_value #>> '{}');
    elsif jsonb_typeof(v_value) in ('number', 'boolean') then
      -- flat scalars travel as text; the kind builders cast them with
      -- the same checks as parsed report text.
      v_text := btrim(v_value::text);
    else
      raise exception 'MALFORMED: correction field % must be a plain value',
        left(v_key, 40);
    end if;
    if v_text is null or v_text = '' then
      raise exception 'MALFORMED: correction field % must not be blank',
        left(v_key, 40);
    end if;
    if pg_catalog.char_length(v_text) > 200 then
      raise exception 'MALFORMED: correction field % is too long',
        left(v_key, 40);
    end if;
    v_out := v_out || jsonb_build_object(v_key, v_text);
  end loop;
  return v_out;
end;
$func$;

revoke execute on function public._amose_apply_intake_corrections(text, jsonb, jsonb)
  from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 3. Cross-channel review requests: the WhatsApp queue no longer reuses a
-- foreign-channel inbox snapshot. WhatsApp drafts keep the exact existing
-- snapshot routing when it authorizes; drafts that arrived on another
-- channel resolve the scope's own single enabled WhatsApp account (zero
-- or several configured accounts report reviewer_unroutable instead of
-- failing). CREATE OR REPLACE preserves the service_role-only grant.
-- ---------------------------------------------------------------------------
create or replace function public.amose_queue_review_requests(
  p_submission_id uuid,
  p_request_key text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text; v_payload jsonb; v_inbox uuid; v_parsed jsonb;
  v_provider text; v_acct_count int;
  v_key text;
  v_ref text;
  v_acct text;
  v_msg text;
  v_notified int := 0;
  v_eligible int := 0;
  v_skipped jsonb := '[]'::jsonb;
  v_preexisting boolean := false;
  v_attempts int := 0;
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

  -- Lock first so concurrent queue attempts serialize on this row.
  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.payload, s.inbox_id, s.provider
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_payload, v_inbox, v_provider
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
      'payment', 'expense', 'cash_handover', 'bank_deposit',
      'stock', 'customer_payment', 'customer_debt') then
    raise exception 'UNSUPPORTED_KIND: submission % has kind %, which needs no human review',
      v_sub, v_kind;
  end if;
  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;

  -- Issue (or reuse) the single reference for this draft.
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
            v_ref, 'queue:' || v_key);
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

  -- Outcome discipline (never report success when nobody was notified):
  -- no_eligible_reviewer (no active authorized reviewer besides the
  -- reporter), reviewer_unroutable (authorization exists but no
  -- authoritative WhatsApp identity or provider-account routing),
  -- queued (at least one new notification), already_queued (idempotent
  -- retry: notifications already exist, nothing duplicated). The draft
  -- stays draft in every non-error case, so it remains safely reviewable
  -- and requeueable after configuration is corrected.
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
      'notified', 0,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;

  -- Authoritative routing: WhatsApp review requests travel on the
  -- business's own WhatsApp number. Drafts that arrived on WhatsApp keep
  -- the exact existing snapshot routing while it authorizes; drafts from
  -- another channel resolve the scope's own single enabled WhatsApp
  -- account instead of reusing a foreign snapshot the outbound guard
  -- would refuse. Missing routing is a reported outcome, not an
  -- exception, so callers can distinguish it from a failure.
  if v_inbox is null then
    return jsonb_build_object('status', 'reviewer_unroutable',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', 0,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;
  select nullif(btrim(i.provider_account), '') into v_acct
    from public.biz_message_inbox i
    where i.id = v_inbox;
  if v_acct is null
      or not public._amose_provider_account_authorized(
        v_tenant, v_business, v_branch, 'whatsapp', v_acct) then
    v_acct := null;
    if v_provider is not null and v_provider is distinct from 'whatsapp' then
      -- Deterministic scope account, never a guess: exactly one enabled
      -- WhatsApp account in this scope wins.
      select count(*), min(a.provider_account) into v_acct_count, v_acct
        from public.biz_provider_accounts a
        where a.tenant_id = v_tenant and a.business_id = v_business
          and a.branch_id = v_branch and a.provider = 'whatsapp'
          and a.enabled = true;
      if v_acct_count <> 1 then
        v_acct := null;
      end if;
    end if;
  end if;
  if v_acct is null then
    return jsonb_build_object('status', 'reviewer_unroutable',
      'review_ref', v_ref,
      'submission_id', v_sub::text,
      'submission_kind', v_kind,
      'request_key', v_key,
      'notified', 0,
      'skipped', '[]'::jsonb,
      'is_retry', false);
  end if;

  -- A re-run that already notified reviewers is idempotent: per-reviewer
  -- keys make re-inserts no-ops, so late-granted reviewers still get
  -- notified while nobody is ever double-notified.
  select exists (select 1 from public.biz_outbound_messages o
      where o.idempotency_key
        like 'review_req:' || v_tenant::text || ':' || v_ref || ':%')
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
        and i.provider = 'whatsapp'
      order by i.provider_sender
      limit 1;
    if v_sender is null then
      v_skipped := v_skipped || jsonb_build_object(
        'employee_id', r.employee_id::text, 'reason', 'no_whatsapp_identity');
      continue;
    end if;
    begin
      insert into public.biz_outbound_messages
        (tenant_id, business_id, branch_id, recipient_employee_id,
         provider, provider_sender, provider_account,
         related_task_id, message_type, message_text, status, idempotency_key)
        values (v_tenant, v_business, v_branch, r.employee_id,
          'whatsapp', v_sender, v_acct,
          null, 'review_request', v_msg,
          'queued', 'review_req:' || v_tenant::text || ':' || v_ref
            || ':' || r.employee_id::text);
      v_notified := v_notified + 1;
    exception when unique_violation then
      -- Already notified on an earlier run: not an error, not a duplicate.
      null;
    end;
  end loop;

  -- Skipped reporters are worth naming (they hold grants but filed this).
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

  if v_notified > 0 then
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
    select count(*) into v_notified
      from public.biz_outbound_messages o
      where o.idempotency_key
        like 'review_req:' || v_tenant::text || ':' || v_ref || ':%';
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
    'notified', 0,
    'skipped', v_skipped,
    'is_retry', false);
end;
$func$;

-- ---------------------------------------------------------------------------
-- 4. Chat-confirm accepts correction values. The 5-argument signature
-- is dropped so no stale builder survives; the new 6-argument form
-- keeps a default so existing 5-argument callers keep working.
-- CREATE OR REPLACE preserves the service_role-only grant, restated
-- below for the new signature.
-- ---------------------------------------------------------------------------
drop function if exists
  public.amose_review_confirm_command(text, text, text, text, text);

create or replace function public.amose_review_confirm_command(
  p_review_ref text,
  p_reviewer_provider text,
  p_reviewer_sender text,
  p_request_key text,
  p_correction_reason text default null,
  p_corrections jsonb default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_reviewer uuid;
  v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text; v_payload jsonb; v_inbox uuid;
  v_ref text; v_key text; v_reason text;
  v_parsed jsonb; v_fields jsonb; v_msg_text text;
  v_unit text; v_qty numeric; v_price numeric;
  v_product uuid; v_product_count int;
  v_amount bigint; v_method text; v_text text;
  v_good int; v_rejected int; v_prod_date date; v_shift text;
  v_date_text text; v_received timestamptz;
  v_normal int; v_cold int;
  v_customer uuid; v_customer_id uuid;
  v_employee uuid;
  v_category text; v_desc text;
  v_dest text; v_deposit_ref text; v_depositor uuid;
  v_from uuid; v_to uuid;
  v_sale_ref text; v_sale_sub uuid; v_sale uuid;
  v_verified jsonb;
  v_corrections jsonb;
begin
  v_ref := nullif(btrim(p_review_ref), '');
  if v_ref is null or v_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_reason := nullif(btrim(p_correction_reason), '');
  if v_reason is not null and pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: correction reason is too long';
  end if;

  select o.o_tenant_id, o.o_employee_id into v_tenant, v_reviewer
    from public._amose_resolve_reviewer_identity(
      p_reviewer_provider, p_reviewer_sender) as o;

  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.payload, s.inbox_id
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_payload, v_inbox
    from public.biz_submissions s
    join public.biz_review_cases c
      on c.tenant_id = v_tenant
      and c.review_ref = v_ref
      and s.tenant_id = c.tenant_id
      and s.business_id = c.business_id
      and s.branch_id = c.branch_id
      and s.id = c.submission_id
    for update of s;
  if not found then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  perform public._amose_authorize_reviewer(
    v_reviewer, v_tenant, v_business, v_branch, v_submitter, 'confirm');

  v_parsed := case when jsonb_typeof(v_payload -> 'parsed') = 'object'
    then v_payload -> 'parsed' else '{}'::jsonb end;
  v_fields := case when jsonb_typeof(v_parsed -> 'fields') = 'object'
    then v_parsed -> 'fields' else '{}'::jsonb end;
  v_msg_text := case when jsonb_typeof(v_payload) = 'object'
    then nullif(btrim(v_payload ->> 'message_text'), '') else null end;

  -- Real corrections: allowlisted textual overrides applied to the
  -- parsed fields BEFORE the kind builders run, so a corrected value
  -- flows through the same casts and checks as reported text.
  -- Corrections require a reason (the audit invariant that
  -- corrections explain themselves is unchanged).
  v_corrections := public._amose_apply_intake_corrections(
    v_kind, v_fields, p_corrections);
  if v_corrections <> '{}'::jsonb then
    if v_reason is null then
      raise exception 'MALFORMED: correction values require a correction reason';
    end if;
    v_fields := v_fields || v_corrections;
  end if;

  if v_kind = 'sale' then
    v_unit := nullif(btrim(v_fields ->> 'unit'), '');
    if v_unit is null then
      raise exception 'MALFORMED: parsed sale has no unit to match a product';
    end if;
    begin
      v_qty := (v_fields ->> 'quantity')::numeric;
      v_price := (v_fields ->> 'unit_price')::numeric;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed sale amounts are not numeric';
    end;
    if v_qty is null or v_price is null
        or v_qty <> trunc(v_qty) or v_price <> trunc(v_price) then
      raise exception 'MALFORMED: parsed sale amounts are not whole numbers';
    end if;
    if v_qty < 1 or v_qty > 2147483647 then
      raise exception 'MALFORMED: parsed sale quantity is out of range';
    end if;
    if v_price < 0 or v_price > 9223372036854775807 then
      raise exception 'MALFORMED: parsed sale price is out of range';
    end if;
    -- Deterministic product match, never a guess: exactly one active product
    -- with this base unit in the submission's own business.
    select count(*) into v_product_count
      from public.biz_products p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.base_unit = v_unit and p.is_active = true;
    if v_product_count <> 1 then
      raise exception 'MALFORMED: no single active product matches the reported unit';
    end if;
    select p.id into v_product
      from public.biz_products p
      where p.tenant_id = v_tenant and p.business_id = v_business
        and p.base_unit = v_unit and p.is_active = true;

    v_verified := jsonb_build_object('kind', 'sale', 'lines',
      jsonb_build_array(jsonb_build_object(
        'product_id', v_product::text,
        'quantity', v_qty::int,
        'unit_price_kobo', v_price::bigint,
        'storage_state', 'normal')));

  elsif v_kind = 'production' or v_kind = 'poultry_daily_report' then
    v_product := public._amose_resolve_intake_product(v_tenant, v_business);
    begin
      v_good := (v_fields ->> 'good_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed production has no good quantity';
    end;
    if v_good is null or v_good < 0 or v_good > 2147483647 then
      raise exception 'MALFORMED: parsed production has no good quantity';
    end if;
    v_rejected := 0;
    if nullif(v_fields ->> 'rejected_quantity', '') is not null then
      begin
        v_rejected := (v_fields ->> 'rejected_quantity')::int;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: parsed production has invalid rejected quantity';
      end;
      if v_rejected is null or v_rejected < 0 then
        raise exception 'MALFORMED: parsed production has invalid rejected quantity';
      end if;
    end if;
    -- Production date: staff-stated first, then the inbox received date
    -- (authoritative system fact), then today. Never invented.
    v_prod_date := null;
    v_date_text := nullif(btrim(v_fields ->> 'production_date'), '');
    if v_date_text is not null then
      begin
        v_prod_date := v_date_text::date;
      exception when others then
        v_prod_date := null;
      end;
    end if;
    if v_prod_date is null and v_inbox is not null then
      select i.received_at into v_received
        from public.biz_message_inbox i
        where i.id = v_inbox;
      if found and v_received is not null then
        v_prod_date := v_received::date;
      end if;
    end if;
    if v_prod_date is null then
      v_prod_date := pg_catalog.now()::date;
    end if;
    -- Documented default shift: daily staff reports cover the full day
    -- unless a shift was stated.
    v_shift := nullif(btrim(v_fields ->> 'shift'), '');
    if v_shift is null or v_shift not in ('morning', 'afternoon', 'night', 'full_day') then
      v_shift := 'full_day';
    end if;

    v_verified := jsonb_build_object('kind', v_kind,
      'product_id', v_product::text,
      'good_quantity', v_good,
      'rejected_quantity', v_rejected,
      'production_date', v_prod_date::text,
      'shift', v_shift);

  elsif v_kind = 'payment' then
    v_sale_ref := nullif(btrim(v_fields ->> 'sale_ref'), '');
    if v_sale_ref is null
        or v_sale_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
      raise exception 'MALFORMED: parsed payment has no sale reference to pay against';
    end if;
    -- The referenced sale must be a posted sale of this tenant/scope:
    -- resolve the reference to its submission, then to its sale.
    select s2.id into v_sale_sub
      from public.biz_review_cases c
      join public.biz_submissions s2
        on s2.tenant_id = c.tenant_id
        and s2.business_id = c.business_id
        and s2.branch_id = c.branch_id
        and s2.id = c.submission_id
      where c.tenant_id = v_tenant
        and c.review_ref = v_sale_ref;
    if not found then
      raise exception 'MALFORMED: referenced sale % is not known here', left(v_sale_ref, 13);
    end if;
    select s3.id into v_sale
      from public.biz_sales s3
      where s3.tenant_id = v_tenant and s3.business_id = v_business
        and s3.branch_id = v_branch
        and s3.source_submission_id = v_sale_sub
        and s3.status in ('confirmed', 'partially_paid', 'paid');
    if not found then
      raise exception 'MALFORMED: referenced sale % is not posted yet; confirm its sale first', left(v_sale_ref, 13);
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed payment has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed payment has no amount';
    end if;
    v_method := nullif(btrim(v_fields ->> 'method'), '');
    if v_method is null
        or v_method not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
      raise exception 'MALFORMED: parsed payment has no valid method; state cash, transfer, or pos';
    end if;

    v_verified := jsonb_build_object('kind', 'payment',
      'sale_id', v_sale::text,
      'amount_kobo', v_amount,
      'method', v_method);

  elsif v_kind = 'expense' then
    v_category := nullif(btrim(v_fields ->> 'category'), '');
    if v_category is null or v_category not in ('fuel', 'maintenance',
        'salaries', 'transport', 'packaging', 'utilities', 'rent',
        'purchases', 'other', 'task_force', 'atwap_dues',
        'tricycle_service') then
      raise exception 'MALFORMED: parsed expense has no valid category';
    end if;
    v_desc := nullif(btrim(v_fields ->> 'description'), '');
    if v_desc is null then
      v_desc := v_msg_text;
    end if;
    if v_desc is null then
      raise exception 'MALFORMED: parsed expense has no description';
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed expense has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed expense has no amount';
    end if;
    -- Documented default method: unstated staff spending is cash unless
    -- the report says otherwise.
    v_method := nullif(btrim(v_fields ->> 'payment_method'), '');
    if v_method is null or v_method not in ('cash', 'transfer', 'pos', 'other') then
      v_method := 'cash';
    end if;

    v_verified := jsonb_build_object('kind', 'expense',
      'category', v_category,
      'description', left(v_desc, 500),
      'amount_kobo', v_amount,
      'payment_method', v_method);

  elsif v_kind = 'cash_handover' then
    v_from := public._amose_resolve_intake_employee(
      v_tenant, v_business, v_branch, v_fields ->> 'from_name');
    v_to := public._amose_resolve_intake_employee(
      v_tenant, v_business, v_branch, v_fields ->> 'to_name');
    if v_from = v_to then
      raise exception 'MALFORMED: handover parties must differ; self-handover is not allowed';
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed handover has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed handover has no amount';
    end if;

    v_verified := jsonb_build_object('kind', 'cash_handover',
      'from_employee_id', v_from::text,
      'to_employee_id', v_to::text,
      'amount_kobo', v_amount);

  elsif v_kind = 'bank_deposit' then
    -- Documented fallback: a deposit report naming no depositor reads as
    -- the reporter's own deposit; the reviewer still authorizes it.
    v_text := nullif(btrim(v_fields ->> 'depositor_name'), '');
    if v_text is not null then
      v_depositor := public._amose_resolve_intake_employee(
        v_tenant, v_business, v_branch, v_text);
    else
      v_depositor := v_submitter;
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed deposit has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed deposit has no amount';
    end if;
    v_dest := nullif(btrim(v_fields ->> 'destination_account'), '');
    if v_dest is null then
      raise exception 'MALFORMED: parsed deposit has no destination account; state where it was deposited';
    end if;
    v_deposit_ref := nullif(btrim(v_fields ->> 'reference'), '');
    if v_deposit_ref is null then
      raise exception 'MALFORMED: parsed deposit has no reference; a deposit is never confirmed without a reference';
    end if;

    v_verified := jsonb_build_object('kind', 'bank_deposit',
      'deposited_by', v_depositor::text,
      'amount_kobo', v_amount,
      'destination_account', left(v_dest, 200),
      'reference', left(v_deposit_ref, 200));

  elsif v_kind = 'stock' then
    v_product := public._amose_resolve_intake_product(v_tenant, v_business);
    begin
      v_normal := (v_fields ->> 'normal_quantity')::int;
      v_cold := (v_fields ->> 'cold_quantity')::int;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed stock count needs both normal and cold quantities';
    end;
    if v_normal is null or v_normal < 0 or v_cold is null or v_cold < 0 then
      raise exception 'MALFORMED: parsed stock count needs both normal and cold quantities';
    end if;

    v_verified := jsonb_build_object('kind', 'stock',
      'product_id', v_product::text,
      'normal_quantity', v_normal,
      'cold_quantity', v_cold);

  elsif v_kind = 'customer_payment' then
    v_customer := null;
    v_text := nullif(v_fields ->> 'customer_id', '');
    if v_text is not null then
      begin
        v_customer_id := v_text::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: parsed customer payment has invalid customer_id';
      end;
      select c.id into v_customer
        from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer_id and c.is_active = true
          and (c.branch_id is null or c.branch_id = v_branch);
      if not found then
        raise exception 'SCOPE: customer is not an active customer of this tenant/business/branch';
      end if;
    else
      v_customer := public._amose_resolve_intake_customer(
        v_tenant, v_business, v_branch, v_fields ->> 'customer_name');
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed customer payment has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed customer payment has no amount';
    end if;
    v_method := nullif(btrim(v_fields ->> 'method'), '');
    if v_method is null
        or v_method not in ('cash', 'transfer', 'pos', 'credit_adjustment') then
      raise exception 'MALFORMED: parsed customer payment has no valid method; state cash, transfer, or pos';
    end if;

    v_verified := jsonb_build_object('kind', 'customer_payment',
      'customer_id', v_customer::text,
      'amount_kobo', v_amount,
      'method', v_method);

  elsif v_kind = 'customer_debt' then
    v_customer := null;
    v_text := nullif(v_fields ->> 'customer_id', '');
    if v_text is not null then
      begin
        v_customer_id := v_text::uuid;
      exception when invalid_text_representation then
        raise exception 'MALFORMED: parsed customer debt has invalid customer_id';
      end;
      select c.id into v_customer
        from public.biz_customers c
        where c.tenant_id = v_tenant and c.business_id = v_business
          and c.id = v_customer_id and c.is_active = true
          and (c.branch_id is null or c.branch_id = v_branch);
      if not found then
        raise exception 'SCOPE: customer is not an active customer of this tenant/business/branch';
      end if;
    else
      v_customer := public._amose_resolve_intake_customer(
        v_tenant, v_business, v_branch, v_fields ->> 'customer_name');
    end if;
    begin
      v_amount := (v_fields ->> 'amount_kobo')::bigint;
    exception when invalid_text_representation then
      raise exception 'MALFORMED: parsed customer debt has no amount';
    end;
    if v_amount is null or v_amount <= 0 then
      raise exception 'MALFORMED: parsed customer debt has no amount';
    end if;

    v_verified := jsonb_build_object('kind', 'customer_debt',
      'customer_id', v_customer::text,
      'amount_kobo', v_amount);

  else
    raise exception 'UNSUPPORTED_KIND: submission kind % cannot be confirmed from chat', v_kind;
  end if;

  return public.amose_confirm_submission(
    v_ref, p_reviewer_provider, p_reviewer_sender,
    v_verified, v_key, v_reason);
end;
$func$;

revoke execute on function public.amose_review_confirm_command(
  text, text, text, text, text, jsonb)
  from public, anon, authenticated;
grant execute on function public.amose_review_confirm_command(
  text, text, text, text, text, jsonb)
  to service_role;
-- ---------------------------------------------------------------------------
-- 5. Submitter cancellation: the linked reporter withdraws their own
-- pending draft. Anyone else -- reviewers, other tenants, strangers --
-- gets UNAUTHORIZED (the same generic message, so callers cannot
-- distinguish "unknown sender" from "not your draft" from "wrong
-- tenant"). Terminal rows fail closed; identical retries return the
-- original result; the same key on different data fails closed.
-- Cancellation is fully distinct from reviewer rejection: status
-- 'cancelled' (not 'rejected'), reporter actor (not reviewer), reason
-- optional (not required), no verified snapshot or posting type, and a
-- review_cancelled acknowledgement to the reporter.
-- ---------------------------------------------------------------------------
create function public.amose_cancel_submission(
  p_review_ref text,
  p_requester_provider text,
  p_requester_sender text,
  p_request_key text,
  p_reason text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_tenant uuid; v_requester uuid;
  v_business text; v_branch text; v_sub uuid; v_submitter uuid;
  v_kind text; v_status text; v_inbox uuid;
  v_case_id uuid; v_case_status text; v_ref text;
  v_key text; v_reason text;
  v_audit_id uuid;
  v_out jsonb; v_existing jsonb;
  v_ack_provider text; v_ack_sender text; v_acct text;
begin
  v_ref := nullif(btrim(p_review_ref), '');
  if v_ref is null or v_ref !~ '^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$' then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;
  v_key := nullif(btrim(p_request_key), '');
  if v_key is null or pg_catalog.char_length(v_key) > 128 then
    raise exception 'MALFORMED: request key must be 1..128 characters';
  end if;
  v_reason := nullif(btrim(p_reason), '');
  if v_reason is not null and pg_catalog.char_length(v_reason) > 2000 then
    raise exception 'MALFORMED: cancellation reason is too long';
  end if;

  -- Requester identity from the numeric sender link. The tenant comes
  -- from this trusted row, so another tenant's reference reads exactly
  -- like a nonexistent one.
  select o.o_tenant_id, o.o_employee_id into v_tenant, v_requester
    from public._amose_resolve_reviewer_identity(
      p_requester_provider, p_requester_sender) as o;

  -- Resolve the reference to its case and lock it, then lock the
  -- submission so concurrent cancellations serialize on this row.
  select c.id, c.business_id, c.branch_id, c.submission_id, c.status
    into v_case_id, v_business, v_branch, v_sub, v_case_status
    from public.biz_review_cases c
    where c.tenant_id = v_tenant
      and c.review_ref = v_ref
    for update;
  if not found then
    raise exception 'NOT_FOUND: review reference is not known';
  end if;

  select s.tenant_id, s.business_id, s.branch_id, s.id, s.employee_id,
      s.kind, s.status, s.inbox_id
    into v_tenant, v_business, v_branch, v_sub, v_submitter,
      v_kind, v_status, v_inbox
    from public.biz_submissions s
    where s.tenant_id = v_tenant
      and s.business_id = v_business
      and s.branch_id = v_branch
      and s.id = v_sub
    for update;
  if not found then
    raise exception 'INCOMPLETE: review case % has no submission', v_ref;
  end if;

  -- Only the reporter withdraws their own draft. Checked BEFORE the
  -- idempotent retry is returned, so retries also resolve the locked
  -- submission and the requester scope first.
  if v_requester is distinct from v_submitter then
    raise exception 'UNAUTHORIZED: only the reporter can cancel this draft';
  end if;

  -- Identical retry (checked AFTER the lock so a concurrent first attempt
  -- is visible): same tenant-scoped key, same reference, and a
  -- consistent cancelled status returns the original result.
  select a.result into v_existing
    from public.biz_review_audit a
    where a.tenant_id = v_tenant
      and a.request_key = v_key;
  if found then
    if (v_existing ->> 'submission_id') is distinct from v_sub::text
        or (v_existing ->> 'review_ref') is distinct from v_ref
        or (v_existing ->> 'review_action') is distinct from 'cancelled' then
      raise exception 'CONFLICT: request key was already used for a different review';
    end if;
    if v_status is distinct from 'cancelled' then
      raise exception 'INCOMPLETE: submission % has a cancellation record but status %', v_sub, v_status;
    end if;
    return (v_existing || jsonb_build_object('is_retry', true));
  end if;

  if v_status is distinct from 'draft' then
    raise exception 'NOT_REVIEWABLE: submission % has status %, only draft submissions can be cancelled',
      v_sub, v_status;
  end if;

  -- Authoritative acknowledgement routing BEFORE any state change: the
  -- destination is the reporter (who is also the requester here) and the
  -- provider account is the original inbound snapshot.
  select o.o_provider, o.o_sender, o.o_account
    into v_ack_provider, v_ack_sender, v_acct
    from public._amose_resolve_ack_routing(
      v_tenant, v_submitter, p_requester_provider, v_inbox) as o;

  update public.biz_submissions s
    set status = 'cancelled'
    where s.id = v_sub;

  v_audit_id := pg_catalog.gen_random_uuid();
  v_out := jsonb_build_object('status', 'cancelled',
    'review_action', 'cancelled',
    'submission_kind', v_kind,
    'submission_id', v_sub::text,
    'review_ref', v_ref,
    'request_key', v_key,
    'audit_id', v_audit_id::text,
    'reason', v_reason,
    'is_retry', false);

  insert into public.biz_review_audit
    (id, tenant_id, business_id, branch_id, submission_id, review_ref,
     action, reviewer_employee_id, reason, verified_snapshot,
     posting_type, result, request_key)
    values (v_audit_id, v_tenant, v_business, v_branch, v_sub, v_ref,
      'cancelled', v_requester, v_reason, null,
      null, v_out, v_key);

  update public.biz_review_cases c
    set status = 'cancelled', decided_at = pg_catalog.now()
    where c.id = v_case_id;

  -- Safe acknowledgement through the durable queue only (queued, never
  -- sent here). The idempotency key is tenant-scoped so tenants never
  -- collide.
  insert into public.biz_outbound_messages
    (tenant_id, business_id, branch_id, recipient_employee_id,
     provider, provider_sender, provider_account,
     related_task_id, message_type, message_text, status, idempotency_key)
    values (v_tenant, v_business, v_branch, v_submitter,
      v_ack_provider, v_ack_sender, v_acct,
      null, 'review_cancelled',
      'Review cancelled: your ' || v_kind || ' report for '
        || v_business || '/' || v_branch
        || ' was withdrawn before review. Ref ' || v_ref || '.',
      'queued', 'review_ack:' || v_tenant::text || ':' || v_key);

  return v_out;
end;
$func$;

revoke execute on function public.amose_cancel_submission(text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.amose_cancel_submission(text, text, text, text, text)
  to service_role;
-- ---------------------------------------------------------------------------
-- 6. Guard triggers learn cancellation. The self-review guard exempts
-- action 'cancelled' (the reporter-actor is the entire point of
-- cancellation; every other action keeps separation of duties), and the
-- case-immutability guard terminalizes cancelled cases so a withdrawn
-- draft can never be reopened. CREATE OR REPLACE preserves the
-- owner-only revokes, restated below for the record.
-- ---------------------------------------------------------------------------
create or replace function public._amose_guard_review_audit_no_self_review()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
declare
  v_submitter uuid;
begin
  -- Cancellation is the reporter's own withdrawal, not a review of
  -- their report: exempt it while every other action keeps separation
  -- of duties.
  if NEW.action = 'cancelled' then
    return NEW;
  end if;
  select s.employee_id into v_submitter
    from public.biz_submissions s
    where s.tenant_id = NEW.tenant_id
      and s.business_id = NEW.business_id
      and s.branch_id = NEW.branch_id
      and s.id = NEW.submission_id;
  if not found or v_submitter is null then
    raise exception 'INCOMPLETE: review audit references an unknown submission';
  end if;
  if NEW.reviewer_employee_id = v_submitter then
    raise exception 'UNAUTHORIZED: reviewer could not be authorized';
  end if;
  return NEW;
end;
$func$;

create or replace function public._amose_guard_review_cases_immutable()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $func$
begin
  if TG_OP = 'DELETE' then
    raise exception 'INCOMPLETE: review cases cannot be deleted';
  end if;
  -- Only the lifecycle columns may change; the reference, scope, submission
  -- binding, and issue key are immutable once written.
  if OLD.tenant_id is distinct from NEW.tenant_id
      or OLD.business_id is distinct from NEW.business_id
      or OLD.branch_id is distinct from NEW.branch_id
      or OLD.submission_id is distinct from NEW.submission_id
      or OLD.review_ref is distinct from NEW.review_ref
      or OLD.request_key is distinct from NEW.request_key then
    raise exception 'INCOMPLETE: review case identity is immutable';
  end if;
  if OLD.status = 'confirmed' or OLD.status = 'rejected'
      or OLD.status = 'cancelled' then
    if NEW.status is distinct from OLD.status then
      raise exception 'NOT_REVIEWABLE: a decided review case cannot be reopened';
    end if;
  end if;
  return NEW;
end;
$func$;

revoke execute on function public._amose_guard_review_audit_no_self_review()
  from public, anon, authenticated, service_role;
revoke execute on function public._amose_guard_review_cases_immutable()
  from public, anon, authenticated, service_role;

commit;
