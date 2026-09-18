# Telegram review-backup channel

Telegram is a channel adapter, not a second business workflow. Reviewers who
cannot reach WhatsApp still receive review requests and approve through
Telegram; every approval, rejection, or correction executes through the
existing Phase 3 RPCs (`amose_confirm_submission` / `amose_reject_submission`)
and the Phase 4 chat-confirm RPC (`amose_review_confirm_command`).
No posting logic is duplicated and the adapter never writes operational or
Brain records. WhatsApp behavior is unchanged.

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

Inbound Telegram text that is not `/start` (linking) or a strict
`REVIEW CONFIRM` / `REVIEW REJECT` command receives a help reply and creates
no submissions. Telegram never ingests reports: drafts are still created
from WhatsApp intake only.

## Render configuration

`TELEGRAM_BOT_TOKEN`: the Bot API token from BotFather, entered privately
(`sync: false`). `TELEGRAM_WEBHOOK_SECRET`: the existing secret already
stored in Render, entered privately (`sync: false`). Never generate or
replace either secret in Render; copy the existing webhook secret privately
into the `setWebhook` call below. `SUPABASE_URL` / `SUPABASE_SECRET_KEY`:
same project as the backend.
Apply `supabase/migrations/20260918155717_telegram_review_backup.sql`
before delivery tests. Never commit, print, or log tokens or secrets.

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

1. Grant reviewer capability first (existing admin boundary):
   `amose_grant_reviewer` for the employee/scope, as with WhatsApp.
2. Link the reviewer's Telegram account: call `/internal/telegram-link`,
   deliver the returned `link_url` to the reviewer over an already-trusted
   channel; they open it and press START within 24 hours. One link works
   once, for one chat id. A chat id already bound to a different employee
   is refused without changes.
3. After WhatsApp intake creates drafts, call
   `/internal/telegram-review-sync` per reviewable submission (or on a
   scheduler). Reviewers with a linked Telegram identity get the request
   with Approve/Reject buttons; Approve executes the standard chat-confirm
   (sale reports only, like WhatsApp); Reject and Correct complete over
   strict text commands carrying the server-issued reference and key, so
   rejections always carry an explicit reason.
4. If Telegram delivery is down, WhatsApp review is unaffected, and
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
- The `Approve` button runs the chat-confirm path, which only supports
  `sale` reports end-to-end today; other kinds fall back to text-command
  rejection or API review, exactly as on WhatsApp.
