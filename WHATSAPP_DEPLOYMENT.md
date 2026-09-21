# WhatsApp Live Activation Runbook

One pipeline only: signed webhook → durable inbox → deterministic drafts →
human confirmation → queued outbound → bounded dispatch → delivery-status
sync. There is no second pipeline and no polling loop. Live activation
itself is manual, after this change is reviewed and merged.

Conventions: every value below is a placeholder. Real tokens, phone
numbers, keys, and employee identifiers never appear in source, tests,
docs, logs, or the database beyond their operational homes. Enter
secrets only in the private field indicated for each step.

## 0. Prereqs checklist

- [ ] Meta app with the WhatsApp product added.
- [ ] A dedicated WhatsApp Business number for YAMSI BizLite.
- [ ] Render service created from `render.yaml` (no secrets in the file).
- [ ] Supabase project with all reviewed migrations applied.
- [ ] Owner holds `YAMSI_API_KEY` privately.

## 1. Meta app and number

1. Create (or select) the Meta app, e.g. display name `YAMSI-BizLite`.
2. Add the **WhatsApp** product to the app.
3. Note the **WhatsApp Business Account ID** as `<WABA_ID>`.
4. Add the sender number; note its **phone_number_id** as
   `<PHONE_NUMBER_ID>` and the display number as `<BUSINESS_NUMBER>`.
5. Required permissions for the system user token: `whatsapp_business_messaging`
   (send/receive) and `whatsapp_business_management` (webhook subscription
   management). Grant least privilege; no marketing or unrelated scopes.

## 2. Production token (system user, permanent)

1. In Meta Business Settings create a **system user** (never a personal
   user token), assign it the WhatsApp app with the permissions above,
   and generate a **permanent token** → `<WHATSAPP_ACCESS_TOKEN>`.
2. Store it only in Render's private environment (step 5). It must never
   appear in source, docs, chat, logs, or the database.
3. Token rotation: generate a replacement token, set it in Render,
   restart/verify with a safe outbound test (step 11), then revoke the
   old token. Reads of old queued rows keep working; in-flight sends
   retry through the bounded policy or fail terminally for re-send.

## 3. Webhook callback URL and verify token

1. Choose a private verify token, e.g. `<WHATSAPP_VERIFY_TOKEN>`
   (32+ random characters). Set it in Render env first.
2. Callback URL: `https://<RENDER_SERVICE>.onrender.com/webhooks/whatsapp`.
3. In the app's WhatsApp → Configuration panel, subscribe the callback
   URL with the verify token. Meta issues a GET verification handshake;
   the service compares in constant time and answers only the challenge.
4. Subscribe to the **messages** field (message + delivery-status events).
   No other field is processed; authentic but irrelevant events are
   acknowledged without processing.

## 4. Render environment (private fields only)

| Variable | Value |
|---|---|
| `WHATSAPP_VERIFY_TOKEN` | `<WHATSAPP_VERIFY_TOKEN>` |
| `WHATSAPP_APP_SECRET` | `<WHATSAPP_APP_SECRET>` (App Dashboard → Settings → Basic) |
| `WHATSAPP_ACCESS_TOKEN` | `<WHATSAPP_ACCESS_TOKEN>` (system-user token) |
| `WHATSAPP_GRAPH_API_VERSION` | `v21.0` (only supported value) |
| `WHATSAPP_WEBHOOK_BASE_URL` | `https://<RENDER_SERVICE>.onrender.com` |
| `SUPABASE_URL` | `https://<PROJECT_REF>.supabase.co` |
| `SUPABASE_SECRET_KEY` | `<SERVICE_ROLE_KEY>` (service role, server only) |
| `YAMSI_API_KEY` | `<OWNER_KEY>` (protects all `/internal/*` routes) |

Optional (compiled defaults apply when unset; set only to override):

| Variable | Value |
|---|---|
| `WHATSAPP_ENV` | `production` (default when unset is production; `local`/`test` only for development) |
| `OUTBOUND_HTTP_TIMEOUT_SECONDS` | e.g. `10` (a number between 1 and 120; default 10) |

Legacy `WEBHOOK_BASE_URL` / `PUBLIC_BASE_URL` still work as a fallback
but are deprecated: readiness reports them and asks for the canonical
`WHATSAPP_WEBHOOK_BASE_URL`. Setting both to different values is
reported as conflicting. A tracked `.env.example` (placeholders only)
lists every name; real `.env` files stay git-ignored.

Never use test/default credentials: missing values fail closed
(readiness reports them; sending/handshake refuse). Validate with:

- `GET /health` — public, safe, no config details.
- `GET /internal/whatsapp-readiness` with header
  `x-yamsi-key: <OWNER_KEY>` — per-dimension booleans
  (inbound/outbound/database/live) plus missing-item names. Values never
  appear. Proceed only when `live_ready` is true.
- `GET /internal/whatsapp/readiness` with header
  `x-yamsi-key: <OWNER_KEY>` — production-readiness report: overall
  `ready`, per-dimension status (`database`, `provider`, `webhook`,
  `outbound_dispatch`), enabled provider-account count for
  `?tenant_id=<TENANT_UUID>`, plus `missing`, `malformed`,
  `conflicting`, `warnings`, and safe remediation naming variable names
  only. No tokens, secrets, phone numbers, or database URLs ever appear.
  Proceed only when `ready` is true.

## 5. Supabase migrations

Apply reviewed migrations in exact filename order (forward-only; the
live database is never reset):

```
20260918061326_human_confirmation_workflow.sql
20260918073012_whatsapp_review_integration.sql
20260918155717_telegram_review_backup.sql
20260919090000_telegram_naira_format.sql
20260920000000_daily_business_records.sql
20260921000000_live_whatsapp_integration.sql
```

Procedure:

```
supabase migration list     # confirm pending set
supabase db push            # or the hosted-SQL equivalent
```

Then verify against a local shadow database (never production data):

```
supabase db reset --local
(pipe each file under supabase/probes/ into the local postgres;
 every file ends in ROLLBACK and a PASSED notice)
```

Probe files: `daily_business_records_probes.sql`,
`live_whatsapp_probes.sql` (registry, claim lease, delivery
monotonicity, branch clarification), `branch_clarification_probes.sql`,
`production_readiness_probes.sql` (per-tenant enabled-account counts
behind the readiness endpoint). All probe writes roll back with zero
residue; a missing PASSED notice is a deployment blocker.

Includes the live-integration migration: `biz_provider_accounts`
registry, outbound lease/retry/delivery columns, stale-claim recovery
RPC (`amose_reclaim_stale_outbound`), and monotonic delivery-status RPC
(`amose_apply_delivery_status`), all service-role-only.

## 6. Register the authorized provider-account mapping

A Meta signature proves the app sent the request; it does NOT authorize
the number. Register each business number for its exact scope (service
role, private session):

```sql
insert into public.biz_provider_accounts
  (tenant_id, business_id, branch_id, provider, provider_account,
   enabled, label)
values
  ('<TENANT_UUID>', 'amose_table_water', 'asaba', 'whatsapp',
   '<PHONE_NUMBER_ID>', true, 'Asaba main line');
```

Rules: one account maps to exactly one tenant/business/branch
(database-enforced); unknown, disabled, or cross-scope accounts fail
closed at ingestion, queueing, and claim time. Verify with
`GET /internal/provider-accounts?tenant_id=<TENANT_UUID>`. To revoke a
number, set `enabled = false` (never delete) — queued rows stop sending
at claim time.

## 7. Staff sender identities

```sql
insert into public.biz_sender_identities
  (tenant_id, provider, provider_sender, employee_id)
values
  ('<TENANT_UUID>', 'whatsapp', '<STAFF_NUMBER>', '<EMPLOYEE_UUID>');
```

Plus `biz_assignments` rows binding the employee to their
business/branch. Unknown senders stay unmatched (clarification queued,
nothing submitted). Multi-branch senders must prefix `<branch>:` when
ambiguous. Reviewer authorization and separation of duties are enforced
in the confirmation RPCs as before.

## 8. Approved bank-deposit accounts and operator piece rate

```sql
insert into public.biz_setting_versions
  (tenant_id, business_id, branch_id, key, effective_from, value)
values
  ('<TENANT_UUID>', 'amose_table_water', 'asaba',
   'approved_bank_deposit_accounts', now(),
   '{"accounts": [{"name": "<BANK_NAME>", "reference": "<STABLE_ID>"}]}'),
  ('<TENANT_UUID>', 'amose_table_water', 'asaba',
   'operator_piece_rate_per_bag', now(),
   '{"amount_kobo": <RATE_KOBO>}');
```

Deposits to unapproved accounts fail closed at confirmation; operator
pay stays a preview until separately confirmed.

## 9. Safe inbound test

1. From a registered staff number, send one water-business message, e.g.
   a sale report for the AMOSE scope.
2. Expect: webhook 200, one `biz_message_inbox` row (`status=received`,
   provider event id stored once -- resends deduplicate), one **draft**
   submission, one review request queued. No financial or operational
   record is created (raw messages create drafts only).
3. Confirm via `REVIEW CONFIRM <YR-REF> KEY <unique-key>` (sale) or API
   review (other kinds) → exactly one atomic posting → confirmation ack
   queued to the original sender on the original provider account.

## 10. Safe outbound test

Safe dispatcher invocation rules (every entry point is bounded; no
polling loop or scheduler exists anywhere):

- Webhook deliveries trigger one background pass of at most 10 rows;
  dispatch trouble is recorded on the rows and never fails the webhook
  acknowledgement.
- Manual backlog drain: `POST /internal/dispatch-outbound`
  (`{"limit": 5}`) with the owner key. `limit` is clamped to 1..100
  (default 20). Never wrap this call in a loop or cron without an
  operator watching the queue -- each pass already claims every due row.
- Crash recovery is a separate explicit call:
  `POST /internal/recover-stale-outbound`
  (`{"stale_seconds": 1800, "limit": 50}`); `stale_seconds` must be
  60..86400. Recovery is transactional and idempotent; concurrent runs
  are safe (`SKIP LOCKED`).

Test procedure:

1. Trigger a clarification or review notification (e.g. message from a
   number with two assignments and no branch prefix).
2. Run one bounded pass as above. The conditional claim sends each
   row at most once; the reply uses the row's own provider account --
   never another tenant's or branch's number.
3. A retryable failure (timeout/429/5xx) re-queues with bounded backoff
   (max 5 attempts, 5min doubling capped at 4h); a permanent failure
   (bad recipient/400) fails terminally without looping.
4. Confirm `failure_reason` holds only sanitized `failure=...` codes and
   the application logs show masked recipients (last four digits only).

## 11. Delivery-status updates

Meta `sent` → `delivered` → `read` events correlate by
(provider, provider account, provider message id) inside the row's own
tenant, apply monotonically (late/duplicates never regress; unknown ids
change nothing; `failed` keeps only a sanitized `code:title`), and never
trigger postings or confirmation. Verify a row's `delivery_state` after
the outbound test.

## 12. Rollback / disable procedure

- Stop inbound: remove the webhook subscription in Meta (messages stop;
  already-stored drafts stay drafts, nothing posts by itself).
- Stop a number: `update biz_provider_accounts set enabled=false ...`
  (queued rows stop at claim; drafts unaffected).
- Stop sending: do not call dispatch; rows stay `queued` (or reclaim
  `sending` leases via `POST /internal/recover-stale-outbound`).
- Re-verify after any rollback step with
  `GET /internal/whatsapp/readiness?tenant_id=<TENANT_UUID>`: the
  affected dimension flips to not-ready with a remediation naming the
  variable or account to restore.
- No code rollback path is needed for reads; migrations are
  forward-only -- disabling at Meta/registry is the switch.

## 14. Production smoke-test checklist

Read-only unless marked LIVE. Nothing here creates financial or
operational records (no fake sales, deposits, or postings).

- [ ] `GET /health` returns ok.
- [ ] `GET /internal/whatsapp/readiness?tenant_id=<TENANT_UUID>` with
  the owner key returns `ready: true` and the expected enabled
  provider-account count.
- [ ] `GET /internal/provider-accounts?tenant_id=<TENANT_UUID>` lists
  each live number under its exact business/branch scope.
- [ ] Meta webhook status shows the callback verified (GET handshake
  answered the challenge; a wrong token is denied, never echoed).
- [ ] LIVE inbound: from a registered staff number, send one real
  report; expect webhook 200, one inbox row, one draft, one review
  request queued -- and no posting until a human confirms.
- [ ] LIVE outbound: run one bounded dispatch pass; expect the reply on
  the row's own number, `delivery_state` progressing
  sent → delivered → read, and masked recipients in the logs.
- [ ] Suspected leak or wrong-account drill: rotate the token (step 2)
  or disable the number (step 12), then re-run readiness to confirm.

## 13. Incident recovery procedure

- Webhook 5xx / DB unreachable: Meta retries delivery (durable insert
  is the acknowledgement boundary; processing never starts first).
- Crash mid-dispatch: rows stuck in `sending` past the lease are
  re-queued by `POST /internal/recover-stale-outbound`
  (`{"stale_seconds": 1800, "limit": 50}`); concurrent recovery is safe
  (`SKIP LOCKED`, idempotent re-run).
- Suspected token leak: rotate per step 2, then inspect
  `biz_outbound_messages.failure_reason` (sanitized codes only -- tokens
  never stored) for the affected window.
- Wrong-account suspicion: query `biz_provider_accounts` for the scope,
  disable the number, and audit `biz_submissions.provider_account` /
  `biz_outbound_messages.provider_account` snapshots.
