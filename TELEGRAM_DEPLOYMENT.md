# Telegram staff-report intake and review channel

Telegram is a channel adapter, not a second business workflow. Linked staff
submit business reports through `@YamsiBizLiteBot`, and reviewers who
cannot reach WhatsApp still receive review requests and approve through
Telegram; every approval, rejection, or correction executes through the
existing Phase 3 RPCs (`amose_confirm_submission` / `amose_reject_submission`)
and the Phase 4 chat-confirm RPC (`amose_review_confirm_command`).
No posting logic is duplicated and the adapter never writes operational or
Brain records. Intake drafts are created through the shared
message_processor draft helpers with business-scoped rule_engine
extraction, exactly like WhatsApp drafts. WhatsApp behavior is unchanged.

## Routes

`POST /webhooks/telegram`: Telegram Bot API updates. Authenticity is the
`X-Telegram-Bot-Api-Secret-Token` header against `TELEGRAM_WEBHOOK_SECRET`;
missing or incorrect secrets return 403 before the body is parsed.
Bodies are capped at 1 MiB (same as WhatsApp), malformed updates return 400,
duplicate `update_id` values are acknowledged without reprocessing
(transactional dedup on the inbox unique constraint), and inbox-storage
failures return 503 so Telegram retries.

`POST /internal/telegram-link` (owner `YAMSI_API_KEY`): mints one one-time,
24-hour link for an employee. The plaintext token is returned exactly once
in `link_url` for the owner to deliver; only its sha256 is stored.

`POST /internal/telegram-review-sync` (owner `YAMSI_API_KEY`): queues
Telegram review notifications for one draft submission
(`{"submission_id": "<uuid>"`, optional `request_key`), then dispatches the
queued notifications with one-tap Approve/Reject buttons.

Inbound Telegram text that is not `/start` (linking), a strict
`REVIEW CONFIRM` / `REVIEW REJECT` command, or a recognized staff report
receives a help reply and creates no submissions. Recognized reports from
linked senders create drafts exactly like WhatsApp intake.

## Staff-report intake

A linked sender (numeric Telegram id bound through the one-time link;
display names and usernames are never trusted) sends one report per
message, starting with one of:

```
SALE 50 bags at 500 cash
PRODUCTION 225 bags used 7kg nylon
EXPENSE fuel 15000
DEPOSIT 80000 bank transfer
STOCK 120 normal bags and 75 cold bags
CUSTOMER PAYMENT Emeka 25000 transfer
CUSTOMER DEBT Ada 12000
```

Flow per message (same shared workflow as WhatsApp):

1. Identity resolves only through `biz_sender_identities`
   (`provider='telegram'`). Unlinked senders get safe linking
   instructions and create no draft.
2. Tenant/business/branch/employee scope resolves from the employee's
   assignments. Senders with several assignments must prefix
   `<branch>:` (e.g. `WARRI: SALE 50 bags at 500 cash`); otherwise they
   get a branch-clarification reply and no draft is created. A branch
   prefix naming no assigned branch is refused the same way, so one
   tenant's sender can never file into another scope.
3. `SALE`/`PRODUCTION`/`EXPENSE`/`DEPOSIT` are normalized to plain
   business wording and parsed by the existing business-scoped
   `rule_engine.extract()`; `STOCK`, `CUSTOMER PAYMENT`, and
   `CUSTOMER DEBT` use the structured `telegram_intake` parsers
   producing the postable `stock` / `customer_payment` / `customer_debt`
   kinds. Nothing is invented: absent details are listed as missing.
4. The report is stored as a `draft` `biz_submissions` row keyed by the
   Telegram `update_id` inbox row (transactional dedup: retries never
   create a second draft), with `provider='telegram'` and
   `provider_account='YamsiBizLiteBot'`.
5. Review requests are queued best-effort on both channels (Telegram
   `tgintake:<inbox_id>`, WhatsApp `queuereq:<inbox_id>`); a queue
   failure never loses the draft.
6. The sender gets the parsed report shown back with anything missing
   plus the existing Confirm / Correct / Cancel review commands
   (`REVIEW CONFIRM <ref> KEY <key>`, with `CORRECTION <reason>` to
   correct, `REVIEW REJECT <ref> KEY <key> REASON <reason>` to cancel).
   To fix a typo the sender simply sends the report again. Only
   authorized reviewers can decide; the reporter cannot confirm their
   own report (separation of duties, enforced in the database).

Kind notes: every kind confirms end-to-end over chat (Approve button
or `REVIEW CONFIRM`, `CORRECTION` to correct, `REVIEW REJECT` with a
reason to cancel) because the verified block is built inside the
database from the submission's own parsed extraction -- no kind needs
API review. `STOCK` posts a branch stock snapshot, `CUSTOMER PAYMENT`
posts a confirmed customer payment with its custody entry, and
`CUSTOMER DEBT` posts an open customer debt, each with verified Brain
memory. Customer names resolve to exactly one active customer of the
scope (create the customer first); products resolve to the single
active product of the business. Documented chat defaults: rejected 0,
`full_day` shift, cash expense method, reporter as fallback
depositor/receiver, inbox received date for production dates. A deposit
is never confirmed without a reference.

## Render configuration

`TELEGRAM_BOT_TOKEN`: the Bot API token from BotFather, entered privately
(`sync: false`). `TELEGRAM_WEBHOOK_SECRET`: the existing secret already
stored in Render, entered privately (`sync: false`). Never generate or
replace either secret in Render; copy the existing webhook secret privately
into the `setWebhook` call below. `SUPABASE_URL` / `SUPABASE_SECRET_KEY`:
same project as the backend.
Apply all migrations in order, including
`supabase/migrations/20260918155717_telegram_review_backup.sql` and
`supabase/migrations/20260922000000_telegram_full_intake.sql`
(new operational tables, posting RPCs, all-kind review queueing and
chat confirm), before delivery tests. Never commit, print, or log
tokens or secrets.

## Post-deployment webhook registration

Run exactly once after the Render deploy is live, from any machine that
already holds the two secret values. `<TELEGRAM_BOT_TOKEN>` and
`<TELEGRAM_WEBHOOK_SECRET>` are placeholders -- substitute privately, never
commit or log the resulting command. This command only registers delivery;
it is the single permitted remote contact in this procedure (do not call it
from CI).

```sh
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://yamsi-bizlite.onrender.com/webhooks/telegram", "secret_token": "<TELEGRAM_WEBHOOK_SECRET>", "allowed_updates": ["message", "callback_query"], "drop_pending_updates": true}'
```

Expected response: `{"ok": true, "result": true, "description": "Webhook was set"}`.
Verify with a wrong-secret POST (expect 403) and an unsigned POST
(expect 403) against `https://yamsi-bizlite.onrender.com/webhooks/telegram`.

Bot username: `@YamsiBizLiteBot` (public address, not a secret).

## Operator runbook

1. Register the bot account for every in-scope branch (service role,
   once per tenant/business/branch -- submissions carrying the Telegram
   route snapshot are refused without it):
   insert into `biz_provider_accounts`
   `(tenant_id, business_id, branch_id, provider, provider_account, enabled)`
   values `(<tenant>, <business>, <branch>, 'telegram',
   'YamsiBizLiteBot', true)`.
2. Grant reviewer capability first (existing admin boundary):
   `amose_grant_reviewer` for the employee/scope, as with WhatsApp.
3. Link each staff Telegram account: call `/internal/telegram-link`,
   deliver the returned `link_url` over an already-trusted channel; they
   open it and press START within 24 hours. One link works once, for one
   chat id. A chat id already bound to a different employee is refused
   without changes. Staff also need a `biz_assignments` row for every
   branch they may report for.
4. Staff send reports as above; drafts queue Telegram review requests
   automatically (`tgintake:<inbox_id>`), and WhatsApp reviewers are
   notified too (`queuereq:<inbox_id>`). After WhatsApp intake creates
   drafts, call `/internal/telegram-review-sync` per reviewable
   submission (or on a scheduler). Reviewers with a linked Telegram
   identity get the request with Approve/Reject buttons; Approve executes
   the standard chat-confirm, which builds the verified block inside the
   database for every kind; Reject and Correct complete over strict text
   commands carrying the server-issued reference and key, so rejections
   always carry an explicit reason.
5. If Telegram delivery is down, WhatsApp review is unaffected, and
   vice versa: each channel queues and sends independently.

## Privilege matrix (this phase; earlier posture unchanged)

- `biz_telegram_links` / `biz_telegram_callbacks`: RLS ON, no policies;
  no privileges for PUBLIC/anon/authenticated/service_role. All access
  runs inside the `SECURITY DEFINER` RPCs (fixed `search_path`).
- New RPCs (`amose_issue_telegram_link`, `amose_consume_telegram_link`,
  `amose_queue_telegram_review_requests`, `amose_mint_telegram_callback`,
  `amose_consume_telegram_callback`): `EXECUTE` revoked from
  PUBLIC/anon/authenticated; granted to `service_role` only.
- Plaintext link/button tokens never touch the database (sha256 hex only),
  never appear in logs, replies (except the single issuance response), or
  persisted error notes (type names only).

## Remaining risks

- Reviewer phones are the trust edge: anyone holding an unlocked linked
  phone can approve. Mitigate with short link TTLs and prompt revocation
  (`amose_revoke_reviewer`) on role change or device loss.
- Telegram delivery is best-effort after the durable inbox insert; a lost
  outbound send is retried by the operator re-calling the sync endpoint
  (claim-once dispatch prevents double sends).
- The `Approve` button runs the chat-confirm path, which builds the
  verified block inside the database for every kind; text commands
  carry `CORRECTION` reasons and `REASON` rejections for all kinds.
- Customer reports need their customer created first (owner tooling):
  an unknown or ambiguous customer name fails closed with a message
  naming the fix, and the draft stays reviewable for a later retry.
- Telegram drafts require the bot account registered per scope (runbook
  step 1); without it the database refuses the draft insert and the
  inbox row is marked failed for operator reprocessing after
  registration.
