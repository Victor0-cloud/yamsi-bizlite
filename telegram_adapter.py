"""Telegram review-backup channel adapter.

Telegram is a channel adapter, not a second business workflow: this module
performs zero direct writes to submissions, operational, Brain, audit, or
review-decision tables. Every approval, rejection, or correction executes
through the existing boundaries -- review_service (which owns the single
Phase 4 RPC per chat command) and human_confirmation (Phase 3 RPCs) -- with
provider='telegram'. Tenant/business/branch/employee scope always resolves
from locked database rows, never from Telegram-supplied values.

Trust model (fail closed throughout):

- Telegram display names, usernames, phone numbers, chat IDs, callback
  data, and submission IDs supplied by users are NEVER trusted. The only
  Telegram value ever bound to an employee is the numeric sender/chat id,
  and only through consuming a one-time link (amose_consume_telegram_link)
  or through an already-stored biz_sender_identities row resolved inside
  the database.
- Inline-button callback_data carries only a short opaque random token
  (Telegram caps callback_data at 64 bytes). Every parameter (review
  reference, action, request key, scope) resolves server-side in
  amose_consume_telegram_callback; forged, replayed, cross-tenant, or
  stale presses change nothing.
- Request keys for button-driven reviews are stable per token
  ("tgcb-"+token), so a crash between consuming a button and running the
  review RPC is healed by retrying the SAME idempotent RPC -- never by
  minting a second decision.
- No tokens, webhook secrets, or message contents are ever logged,
  echoed in replies, or stored in exception text persisted to the
  database. Replies carry only opaque review references (already known to
  the reviewer) and outcome words.

This module never reads WhatsApp credentials and works independently when
they are unavailable.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx

import human_confirmation
import outbound_telegram
import review_service
from review_service import TERMINAL_REVIEW_ERRORS
from supabase_backend import credentials, DatabaseUnavailable

PROVIDER = "telegram"

# Server-side constant identifying this bot in biz_message_inbox /
# biz_outbound_messages provider_account. Public (it is the bot's address,
# not a secret) and never taken from user input.
BOT_ACCOUNT = "YamsiBizLiteBot"
BOT_DEEP_LINK = "https://t.me/" + BOT_ACCOUNT

MAX_BYTES = 1024 * 1024

LINK_TTL = timedelta(hours=24)
CALLBACK_TTL = timedelta(hours=72)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
NUMERIC_SENDER_RE = re.compile(r"^[0-9]{1,20}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

ISSUE_LINK_RPC = "amose_issue_telegram_link"
CONSUME_LINK_RPC = "amose_consume_telegram_link"
QUEUE_RPC = "amose_queue_telegram_review_requests"
MINT_RPC = "amose_mint_telegram_callback"
CONSUME_RPC = "amose_consume_telegram_callback"

ISSUE_LINK_ALLOWLIST = frozenset({ISSUE_LINK_RPC})
CONSUME_LINK_ALLOWLIST = frozenset({CONSUME_LINK_RPC})
QUEUE_ALLOWLIST = frozenset({QUEUE_RPC})
MINT_ALLOWLIST = frozenset({MINT_RPC})
CONSUME_ALLOWLIST = frozenset({CONSUME_RPC})

QUEUE_STATUSES = frozenset({"queued", "already_queued",
    "no_eligible_reviewer", "reviewer_unroutable"})

HELP_UNLINKED = (
    "YAMSI review backup: this Telegram account is not linked. "
    "Ask your administrator for a one-time link, then open it and press "
    "START. Reviewers approve with the Approve button or with "
    "REVIEW CONFIRM <reference> KEY <key>.")

HELP_LINKED = (
    "YAMSI review backup: linked. You will receive review requests here. "
    "Approve with the Approve button, or reply "
    "REVIEW CONFIRM <reference> KEY <key> "
    "[CORRECTION <reason>], or "
    "REVIEW REJECT <reference> KEY <key> REASON <reason>.")

HELP_LINKED_ORDINARY = (
    "YAMSI review backup: this Telegram account is linked. "
    "This bot accepts review requests and REVIEW commands only -- it does "
    "not take sales, stock, production, or expense reports. "
    "Approve with the Approve button, or reply "
    "REVIEW CONFIRM <reference> KEY <key> "
    "[CORRECTION <reason>], or "
    "REVIEW REJECT <reference> KEY <key> REASON <reason>.")

REJECT_INSTRUCTIONS = (
    "To reject, reply with: REVIEW REJECT {ref} KEY {key} REASON <reason>. "
    "The reason is required and is recorded with the rejection.")

CORRECT_INSTRUCTIONS = (
    "To correct, reply with: REVIEW CONFIRM {ref} KEY {key} "
    "CORRECTION <reason>.")


class TelegramAdapterError(Exception):
    """Base class: the Telegram adapter refused or failed; nothing was
    half-written (each RPC is transactional)."""


class TelegramLinkError(TelegramAdapterError):
    """Link issuance or consumption was refused."""


class TelegramCallbackError(TelegramAdapterError):
    """A button press was refused (forged, stale, unauthorized, expired)."""


# ---------------------------------------------------------------------------
# Pure parsing / token helpers. No I/O, no secrets.
# ---------------------------------------------------------------------------

def sha256_hex(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_link_token():
    return secrets.token_urlsafe(32)


def new_callback_token():
    return secrets.token_urlsafe(24)


def _require_sender_id(value):
    """Numeric Telegram sender/chat ids only. Anything else (usernames,
    display names, phone numbers) is rejected, never bound."""
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and NUMERIC_SENDER_RE.match(value.strip()):
        return value.strip()
    raise TelegramAdapterError("A numeric Telegram sender identity is required")


def parse_update(payload):
    """Strictly validates one Telegram update object. Returns
    {"update_id", "message"?, "callback_query"?} or raises
    TelegramAdapterError. Untrusted display fields are never returned --
    only ids and text needed for routing, resolved against the database
    by the caller."""
    if not isinstance(payload, dict):
        raise TelegramAdapterError("Update must be an object")
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int):
        raise TelegramAdapterError("Update is missing its identifier")
    parsed = {"update_id": update_id}
    message = payload.get("message")
    if message is not None:
        if not isinstance(message, dict):
            raise TelegramAdapterError("Message must be an object")
        parsed["message"] = message
    callback = payload.get("callback_query")
    if callback is not None:
        if not isinstance(callback, dict):
            raise TelegramAdapterError("Callback query must be an object")
        parsed["callback_query"] = callback
    if "message" not in parsed and "callback_query" not in parsed:
        raise TelegramAdapterError("Update carries no supported event")
    return parsed


def provider_event_id(update_id):
    return "update:%d" % update_id


def message_sender(message):
    sender = (message.get("from") or {}).get("id") \
        if isinstance(message.get("from"), dict) else None
    return _require_sender_id(sender)


def message_chat_id(message):
    chat = message.get("chat") or {}
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    return _require_sender_id(chat_id)


def message_text(message):
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > 4096:
        raise TelegramAdapterError("Message text is too long")
    return text


def split_start_token(text):
    """Splits '/start' or '/start <token>' (optionally suffixed with
    @BotName). Returns the token string or None. Never validates the
    token itself -- that happens inside the consume RPC."""
    if not isinstance(text, str):
        return None
    head = text.strip().split(None, 1)
    command = head[0].split("@")[0].lower()
    if command != "/start":
        return None
    if len(head) < 2:
        return None
    token = head[1].strip().split()[0]
    if not TOKEN_RE.match(token):
        raise TelegramAdapterError("Link token has an invalid shape")
    return token


def is_start_command(text):
    if not isinstance(text, str):
        return False
    words = text.strip().split()
    if not words:
        return False
    return words[0].split("@")[0].lower() == "/start"


def callback_parts(callback_query):
    """Extracts (query_id, sender, chat_id, message_id, token) from a
    callback query. The token's meaning resolves server-side only."""
    query_id = callback_query.get("id")
    if not isinstance(query_id, str) or not query_id:
        raise TelegramAdapterError("Callback query is missing its identifier")
    sender = (callback_query.get("from") or {}).get("id") \
        if isinstance(callback_query.get("from"), dict) else None
    clean_sender = _require_sender_id(sender)
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {} if isinstance(message, dict) else {}
    chat_id = _require_sender_id(
        chat.get("id") if isinstance(chat, dict) else None)
    message_id = message.get("message_id") \
        if isinstance(message, dict) else None
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise TelegramAdapterError("Callback query is missing its message")
    data = callback_query.get("data")
    if not isinstance(data, str) or not TOKEN_RE.match(data.strip()):
        raise TelegramAdapterError("Callback data is not valid")
    return query_id, clean_sender, chat_id, message_id, data.strip()


def review_keyboard(approve_token, reject_token):
    return {"inline_keyboard": [
        [{"text": "Approve", "callback_data": approve_token}],
        [{"text": "Reject", "callback_data": reject_token}]]}


# ---------------------------------------------------------------------------
# RPC layer. Each entry point calls exactly one allowlisted RPC over a
# fresh connection. Scope resolves inside the database.
# ---------------------------------------------------------------------------

async def _call_rpc(function_name, allowed, payload):
    if function_name not in allowed:
        raise TelegramAdapterError(
            "Refusing to call non-allowlisted function: " + function_name)
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise human_confirmation.WorkflowDatabaseError(
            "Supabase is not configured: " + str(exc)) from None
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=20, follow_redirects=False) as client:
            response = await client.post("/rest/v1/rpc/" + function_name,
                json=payload)
    except httpx.HTTPError:
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC unreachable: " + function_name) from None
    if response.status_code not in (200, 201):
        message = response.text or ("Review RPC failed: " + function_name)
        raise human_confirmation._classify_rpc_error(
            human_confirmation._extract_rpc_message(response, message))
    try:
        return response.json()
    except ValueError:
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned invalid JSON") from None


def _require_uuid(value, field):
    if not isinstance(value, str) or not UUID_RE.match(value.strip()):
        raise TelegramAdapterError("A %s UUID is required" % field)
    return value.strip()


def _require_request_key(value):
    if not isinstance(value, str) \
            or not human_confirmation.REQUEST_KEY_PATTERN.match(value):
        raise TelegramAdapterError(
            "A request key of 1..128 [A-Za-z0-9:_-] characters is required")
    return value


def _validate_issue_result(result, tenant_id, employee_id, request_key):
    if not isinstance(result, dict) or result.get("status") != "issued":
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned unexpected status")
    if result.get("tenant_id") != tenant_id \
            or result.get("employee_id") != employee_id \
            or result.get("request_key") != request_key:
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned mismatched identifiers")
    if not isinstance(result.get("is_retry"), bool):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC result is missing the retry flag")


def _validate_queue_result(result, submission_id, request_key):
    if not isinstance(result, dict) or result.get("status") not in QUEUE_STATUSES:
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r"
            % ((result or {}).get("status"),))
    ref = result.get("review_ref")
    if not isinstance(ref, str) \
            or not human_confirmation.REVIEW_REF_PATTERN.match(ref):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned a malformed review reference")
    if result.get("submission_id") != submission_id \
            or result.get("request_key") != request_key:
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned mismatched identifiers")
    if not isinstance(result.get("notified"), list) \
            or not isinstance(result.get("skipped"), list):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC result is missing the notified/skipped lists")
    if not isinstance(result.get("is_retry"), bool):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC result is missing the retry flag")


def _validate_consume_result(result, token_hash):
    del token_hash
    if not isinstance(result, dict):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") not in (
            "ready", "already_used", "case_closed", "expired"):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r"
            % (result.get("status"),))
    ref = result.get("review_ref")
    if not isinstance(ref, str) \
            or not human_confirmation.REVIEW_REF_PATTERN.match(ref):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned a malformed review reference")
    if result.get("action") not in ("approve", "reject"):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned an unexpected action")
    _require_request_key(result.get("request_key"))
    if not isinstance(result.get("is_retry"), bool):
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC result is missing the retry flag")


async def issue_employee_link(tenant_id, employee_id, request_key,
        ttl=LINK_TTL):
    """Mints one one-time link for an employee (trusted owner callers
    only). Returns the deep link carrying the plaintext token exactly
    once -- the token is never stored, logged, or returnable again."""
    tenant = _require_uuid(tenant_id, "tenant ID")
    employee = _require_uuid(employee_id, "employee ID")
    key = _require_request_key(request_key)
    token = new_link_token()
    expires_at = datetime.now(timezone.utc) + ttl
    try:
        result = await _call_rpc(ISSUE_LINK_RPC, ISSUE_LINK_ALLOWLIST, {
            "p_tenant_id": tenant, "p_employee_id": employee,
            "p_token_hash": sha256_hex(token),
            "p_request_key": key,
            "p_expires_at": expires_at.isoformat()})
    except human_confirmation.WorkflowDatabaseError:
        raise
    except human_confirmation.WorkflowError as error:
        raise TelegramLinkError(str(error)) from None
    _validate_issue_result(result, tenant, employee, key)
    return {"link_url": "%s?start=%s" % (BOT_DEEP_LINK, token),
        "tenant_id": tenant, "employee_id": employee,
        "request_key": key, "expires_at": result.get("expires_at"),
        "is_retry": result.get("is_retry")}


async def consume_link_token(token, sender):
    """Binds a numeric Telegram sender to the link's employee. Unknown,
    expired, and already-used tokens share one generic refusal."""
    if not isinstance(token, str) or not TOKEN_RE.match(token.strip()):
        raise TelegramLinkError("Link token has an invalid shape")
    clean_sender = _require_sender_id(sender)
    try:
        result = await _call_rpc(CONSUME_LINK_RPC, CONSUME_LINK_ALLOWLIST, {
            "p_token_hash": sha256_hex(token.strip()),
            "p_provider_sender": clean_sender})
    except human_confirmation.WorkflowDatabaseError:
        raise
    except human_confirmation.WorkflowError as error:
        raise TelegramLinkError(str(error)) from None
    if not isinstance(result, dict) or result.get("status") != "linked":
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned unexpected status")
    return result


async def queue_telegram_reviews(submission_id, request_key):
    """Queues Telegram review notifications for an existing draft through
    exactly one atomic RPC. Scope, reference, message content, and routing
    resolve inside the database."""
    submission = _require_uuid(submission_id, "submission ID")
    key = _require_request_key(request_key)
    try:
        result = await _call_rpc(QUEUE_RPC, QUEUE_ALLOWLIST, {
            "p_submission_id": submission, "p_request_key": key,
            "p_bot_account": BOT_ACCOUNT})
    except human_confirmation.WorkflowDatabaseError:
        raise
    except human_confirmation.WorkflowError as error:
        raise TelegramAdapterError(str(error)) from None
    _validate_queue_result(result, submission, key)
    return result


async def mint_button_token(review_ref, action, sender, ttl=CALLBACK_TTL):
    """Mints one opaque button token for (reference, action, reviewer).
    Returns the raw token for the button plus the stable request key the
    press-time review RPC must reuse."""
    clean_ref = human_confirmation._require_review_ref(review_ref)
    if action not in ("approve", "reject"):
        raise TelegramAdapterError("Callback action must be approve or reject")
    clean_sender = _require_sender_id(sender)
    token = new_callback_token()
    request_key = "tgcb-" + token
    expires_at = datetime.now(timezone.utc) + ttl
    try:
        result = await _call_rpc(MINT_RPC, MINT_ALLOWLIST, {
            "p_token_hash": sha256_hex(token),
            "p_review_ref": clean_ref, "p_action": action,
            "p_reviewer_provider": PROVIDER,
            "p_reviewer_sender": clean_sender,
            "p_request_key": request_key,
            "p_expires_at": expires_at.isoformat()})
    except human_confirmation.WorkflowDatabaseError:
        raise
    except human_confirmation.WorkflowError as error:
        raise TelegramCallbackError(str(error)) from None
    if not isinstance(result, dict) or result.get("status") != "minted":
        raise human_confirmation.WorkflowDatabaseError(
            "Review RPC returned unexpected status")
    return {"token": token, "request_key": request_key, "result": result}


async def consume_button_token(token, sender):
    """Resolves a button press server-side. Forged, stale, unauthorized,
    and cross-tenant presses raise without changing anything."""
    if not isinstance(token, str) or not TOKEN_RE.match(token.strip()):
        raise TelegramCallbackError("Callback data is not valid")
    clean_sender = _require_sender_id(sender)
    try:
        result = await _call_rpc(CONSUME_RPC, CONSUME_ALLOWLIST, {
            "p_token_hash": sha256_hex(token.strip()),
            "p_reviewer_provider": PROVIDER,
            "p_reviewer_sender": clean_sender})
    except human_confirmation.WorkflowDatabaseError:
        raise
    except human_confirmation.WorkflowError as error:
        raise TelegramCallbackError(str(error)) from None
    _validate_consume_result(result, token)
    return result


# ---------------------------------------------------------------------------
# Inbox status + outbound dispatch over the shared REST helpers.
# ---------------------------------------------------------------------------

async def _set_inbox_status(client, inbox_id, status, note=None):
    body = {"status": status}
    if note is not None:
        body["processing_error"] = note[:500]
    response = await client.patch("/rest/v1/biz_message_inbox",
        params={"id": "eq." + inbox_id}, json=body,
        headers={"Prefer": "return=minimal"})
    if response.status_code not in (200, 204):
        raise human_confirmation.WorkflowDatabaseError(
            "Unable to update inbox status")


async def _mark_outbound(client, outbound_id, status, extra=None):
    body = {"status": status}
    if extra:
        body.update(extra)
    response = await client.patch("/rest/v1/biz_outbound_messages",
        params={"id": "eq." + outbound_id}, json=body,
        headers={"Prefer": "return=minimal"})
    if response.status_code not in (200, 204):
        raise human_confirmation.WorkflowDatabaseError(
            "Unable to update outbound status")


def _failure_note(error):
    # Persist only stable outcome words / exception type names -- never
    # message contents, tokens, or RPC payloads.
    if isinstance(error, (TelegramLinkError, TelegramCallbackError,
            TelegramAdapterError)):
        return "%s: refused" % type(error).__name__
    if isinstance(error, TERMINAL_REVIEW_ERRORS):
        return "%s: refused" % type(error).__name__
    return "%s: failed" % type(error).__name__


def _new_summary():
    return {"scanned": 0, "links_consumed": 0, "reviews_confirmed": 0,
        "reviews_rejected": 0, "reviews_refused": 0, "reviews_failed": 0,
        "callbacks_answered": 0, "help_sent": 0, "unmatched": 0,
        "notifications_sent": 0, "notifications_failed": 0, "failed": 0}


def _client_headers():
    url, secret = credentials()
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    return url, headers


async def _send_best_effort(summary, chat_id, text, key,
        reply_markup=None):
    try:
        await outbound_telegram.send_message(chat_id, text,
            reply_markup=reply_markup)
    except outbound_telegram.OutboundUnavailable:
        summary["notifications_failed"] += 1
        return False
    summary[key] += 1
    return True


async def _is_linked_sender(client, sender):
    """Returns True when the numeric Telegram sender already has a
    biz_sender_identities row for provider='telegram'. Only the numeric
    sender id is ever bound -- display names, usernames, phone numbers,
    and any tenant/employee/scope values from the message are ignored.
    Transport, status, and payload failures raise WorkflowDatabaseError
    (fail closed) without embedding secrets or identifiers."""
    clean_sender = _require_sender_id(sender)
    try:
        response = await client.get("/rest/v1/biz_sender_identities",
            params={"provider": "eq." + PROVIDER,
                "provider_sender": "eq." + clean_sender,
                "select": "employee_id", "limit": "1"})
    except httpx.HTTPError:
        raise human_confirmation.WorkflowDatabaseError(
            "Unable to resolve sender identity") from None
    if response.status_code != 200:
        raise human_confirmation.WorkflowDatabaseError(
            "Unable to resolve sender identity")
    try:
        rows = response.json()
    except ValueError:
        raise human_confirmation.WorkflowDatabaseError(
            "Unable to resolve sender identity") from None
    if not isinstance(rows, list):
        raise human_confirmation.WorkflowDatabaseError(
            "Unable to resolve sender identity")
    return len(rows) > 0


async def _send_identity_help(client, inbox_id, sender, chat_id, summary):
    """Sends the ordinary-message help reply matching the stored Telegram
    identity: linked reviewers hear review-only scope, genuinely unlinked
    senders hear the linking instructions. Creates no submissions and
    adds no intake of any kind. Database failures propagate (fail closed)
    with no reply and no identifiers exposed."""
    if await _is_linked_sender(client, sender):
        await _send_best_effort(summary, chat_id, HELP_LINKED_ORDINARY,
            "help_sent")
    else:
        await _send_best_effort(summary, chat_id, HELP_UNLINKED, "help_sent")
    await _set_inbox_status(client, inbox_id, "processed")
    summary["unmatched"] += 1
    return {"outcome": "help"}


async def _process_message(client, inbox_id, message, summary):
    sender = message_sender(message)
    chat_id = message_chat_id(message)
    text = message_text(message)
    if text is None:
        return await _send_identity_help(
            client, inbox_id, sender, chat_id, summary)

    # Secure one-time link presentation. An invalid-shape token is a
    # terminal refusal (help reply, row consumed), never a stuck row.
    if is_start_command(text):
        try:
            token = split_start_token(text)
        except TelegramAdapterError:
            token = None
            await _send_best_effort(summary, chat_id,
                HELP_UNLINKED, "help_sent")
            await _set_inbox_status(client, inbox_id, "processed")
            summary["reviews_refused"] += 1
            return {"outcome": "refused", "error": "TelegramAdapterError"}
        if token is None:
            await _send_best_effort(summary, chat_id,
                HELP_UNLINKED, "help_sent")
            await _set_inbox_status(client, inbox_id, "processed")
            return {"outcome": "help"}
        try:
            await consume_link_token(token, sender)
        except (TelegramLinkError,
                human_confirmation.WorkflowDatabaseError,
                human_confirmation.WorkflowError) as error:
            if isinstance(error, human_confirmation.WorkflowDatabaseError):
                await _set_inbox_status(client, inbox_id, "failed",
                    _failure_note(error))
                summary["failed"] += 1
                return {"outcome": "failed", "error": type(error).__name__}
            await _send_best_effort(summary, chat_id,
                "That link is not valid (unknown, expired, or already "
                "used). Ask your administrator for a new one.", "help_sent")
            await _set_inbox_status(client, inbox_id, "processed")
            summary["reviews_refused"] += 1
            return {"outcome": "refused", "error": type(error).__name__}
        await _send_best_effort(summary, chat_id,
            "Linked. You will receive review requests here. " + HELP_LINKED,
            "help_sent")
        await _set_inbox_status(client, inbox_id, "processed")
        summary["links_consumed"] += 1
        return {"outcome": "linked"}

    # Strict REVIEW commands reuse the exact WhatsApp-reviewed boundary
    # with provider='telegram'. Sender identity comes from the message,
    # never from the text.
    if review_service.is_reserved_command(text):
        command = review_service.parse_review_command(text)
        row = {"id": inbox_id, "provider": PROVIDER}
        if command is not None:
            outcome = await review_service.handle_review_command(
                client, row, sender, command, summary)
            status_word = outcome.get("outcome")
            if status_word in ("confirm", "reject"):
                reply = "Recorded %s for %s." % (
                    status_word, command.get("review_ref"))
                await _send_best_effort(summary, chat_id, reply, "help_sent")
            elif status_word == "refused":
                await _send_best_effort(summary, chat_id,
                    "Refused (%s). Send /start for command help."
                    % outcome.get("error"), "help_sent")
            return outcome
        await review_service.refuse_malformed_command(client, row, summary)
        await _send_best_effort(summary, chat_id,
            "Refused: text begins with the reserved review prefix but is "
            "not a valid REVIEW command. Send /start for command help.",
            "help_sent")
        return {"outcome": "refused", "error": "RefusedReviewCommand"}

    return await _send_identity_help(
        client, inbox_id, sender, chat_id, summary)


async def _process_callback(client, inbox_id, callback_query, summary):
    try:
        query_id, sender, chat_id, message_id, token = callback_parts(
            callback_query)
    except TelegramAdapterError as error:
        await _set_inbox_status(client, inbox_id, "processed",
            _failure_note(error))
        summary["reviews_refused"] += 1
        return {"outcome": "refused", "error": type(error).__name__}
    try:
        consumed = await consume_button_token(token, sender)
    except (TelegramCallbackError,
            human_confirmation.WorkflowDatabaseError,
            human_confirmation.WorkflowError) as error:
        await outbound_telegram.answer_callback_query(
            query_id, "That button is no longer valid.")
        summary["callbacks_answered"] += 1
        if isinstance(error, human_confirmation.WorkflowDatabaseError):
            await _set_inbox_status(client, inbox_id, "failed",
                _failure_note(error))
            summary["failed"] += 1
            return {"outcome": "failed", "error": type(error).__name__}
        await _set_inbox_status(client, inbox_id, "processed",
            _failure_note(error))
        summary["reviews_refused"] += 1
        return {"outcome": "refused", "error": type(error).__name__}

    status = consumed.get("status")
    review_ref = consumed.get("review_ref")
    request_key = consumed.get("request_key")
    await outbound_telegram.answer_callback_query(query_id)
    summary["callbacks_answered"] += 1
    if status in ("case_closed", "expired"):
        await outbound_telegram.clear_inline_keyboard(chat_id, message_id)
        await _send_best_effort(summary, chat_id,
            "That request for %s is already decided or expired; no action "
            "was taken." % review_ref, "help_sent")
        await _set_inbox_status(client, inbox_id, "processed")
        summary["reviews_refused"] += 1
        return {"outcome": "refused", "error": status}
    if consumed.get("action") == "reject":
        # Rejections require an explicit reason, so a tap can never
        # reject by itself: the reviewer completes the strict text
        # command carrying the server-issued reference and key.
        await outbound_telegram.clear_inline_keyboard(chat_id, message_id)
        await _send_best_effort(summary, chat_id,
            REJECT_INSTRUCTIONS.format(ref=review_ref, key=request_key),
            "help_sent")
        await _set_inbox_status(client, inbox_id, "processed")
        return {"outcome": "reject_instructions"}
    try:
        await review_service.confirm_from_chat(
            client, review_ref, PROVIDER, sender, request_key)
        summary["reviews_confirmed"] += 1
        outcome = "confirm"
    except TERMINAL_REVIEW_ERRORS as error:
        await _set_inbox_status(client, inbox_id, "processed",
            _failure_note(error))
        summary["reviews_refused"] += 1
        await _send_best_effort(summary, chat_id,
            "Refused (%s) for %s." % (type(error).__name__, review_ref),
            "help_sent")
        return {"outcome": "refused", "error": type(error).__name__}
    except human_confirmation.WorkflowDatabaseError as error:
        await _set_inbox_status(client, inbox_id, "failed",
            _failure_note(error))
        summary["failed"] += 1
        return {"outcome": "failed", "error": type(error).__name__}
    await outbound_telegram.clear_inline_keyboard(chat_id, message_id)
    await _send_best_effort(summary, chat_id,
        "Recorded confirm for %s." % review_ref, "help_sent")
    await _set_inbox_status(client, inbox_id, "processed")
    return {"outcome": outcome}


async def process_stored_update(update, inbox_id, summary=None):
    """Routes one durably-stored Telegram update. Isolated per update: a
    failure marks only this row (failed for transient database errors,
    processed with a refusal note for terminal ones) and never blocks or
    duplicates other updates."""
    summary = summary if summary is not None else _new_summary()
    url, headers = _client_headers()
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=8, follow_redirects=False) as client:
            summary["scanned"] += 1
            try:
                if "callback_query" in update:
                    return await _process_callback(
                        client, inbox_id, update["callback_query"], summary)
                return await _process_message(
                    client, inbox_id, update.get("message") or {}, summary)
            except (TelegramAdapterError, TelegramLinkError,
                    TelegramCallbackError) as error:
                # Pre-routing validation (unresolvable sender/chat, bad
                # identity shape): terminal, consumed with a refusal note
                # so the row is never stuck at received.
                summary["reviews_refused"] += 1
                try:
                    await _set_inbox_status(client, inbox_id, "processed",
                        _failure_note(error))
                except human_confirmation.WorkflowDatabaseError:
                    pass
                return {"outcome": "refused", "error": type(error).__name__}
            except human_confirmation.WorkflowDatabaseError as error:
                summary["failed"] += 1
                try:
                    await _set_inbox_status(client, inbox_id, "failed",
                        _failure_note(error))
                except human_confirmation.WorkflowDatabaseError:
                    pass
                return {"outcome": "failed", "error": type(error).__name__}
            except Exception as error:
                # Unexpected bug: fail the row visibly for manual
                # reprocessing rather than leaving it silently stuck.
                summary["failed"] += 1
                try:
                    await _set_inbox_status(client, inbox_id, "failed",
                        _failure_note(error))
                except human_confirmation.WorkflowDatabaseError:
                    pass
                return {"outcome": "failed", "error": type(error).__name__}
    except DatabaseUnavailable as error:
        summary["failed"] += 1
        return {"outcome": "failed", "error": type(error).__name__}
    return {"outcome": "processed"}


def _parse_outbound_ref(idempotency_key):
    """Parses our own 'tg_review_req:<tenant>:<ref>:<employee>' keys.
    Anything else fails closed (row marked failed, never sent)."""
    if not isinstance(idempotency_key, str):
        return None
    parts = idempotency_key.split(":")
    if len(parts) != 4 or parts[0] != "tg_review_req":
        return None
    _tenant, ref, _employee = parts[1], parts[2], parts[3]
    if not human_confirmation.REVIEW_REF_PATTERN.match(ref):
        return None
    return ref


async def dispatch_queued(limit=20, review_ref=None):
    """Claims queued Telegram review_request rows (exactly once via a
    conditional claim) and sends each with fresh one-tap buttons. Minting
    replaces superseded buttons transactionally, so retries never stack
    live tokens. Returns a summary; never raises for a single-row
    failure."""
    summary = {"scanned": 0, "sent": 0, "failed": 0, "claim_conflicts": 0}
    url, headers = _client_headers()
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=8, follow_redirects=False) as client:
            params = {"provider": "eq." + PROVIDER,
                "status": "eq.queued",
                "message_type": "eq.review_request",
                "order": "queued_at.asc", "limit": str(limit),
                "select": "id,tenant_id,provider_sender,recipient_employee_id,"
                    "message_text,idempotency_key"}
            if review_ref is not None:
                params["idempotency_key"] = "like.*:%s:*" % review_ref
            response = await client.get("/rest/v1/biz_outbound_messages",
                params=params)
            if response.status_code != 200:
                raise human_confirmation.WorkflowDatabaseError(
                    "Unable to read outbound queue")
            rows = response.json()
            for row in rows:
                summary["scanned"] += 1
                claimed = await client.patch(
                    "/rest/v1/biz_outbound_messages",
                    params={"id": "eq." + row["id"],
                        "status": "eq.queued"},
                    json={"status": "sending"},
                    headers={"Prefer": "return=representation"})
                if claimed.status_code not in (200, 201):
                    summary["failed"] += 1
                    continue
                claimed_rows = claimed.json()
                if not claimed_rows:
                    summary["claim_conflicts"] += 1
                    continue
                outcome = await _send_claimed(client, claimed_rows[0])
                if outcome == "sent":
                    summary["sent"] += 1
                else:
                    summary["failed"] += 1
    except (DatabaseUnavailable, human_confirmation.WorkflowDatabaseError,
            httpx.HTTPError):
        pass
    return summary


async def _send_claimed(client, row):
    ref = _parse_outbound_ref(row.get("idempotency_key"))
    chat_id = row.get("provider_sender")
    try:
        if ref is None or not isinstance(chat_id, str) or not chat_id:
            raise TelegramAdapterError("Outbound row is not routable")
        approve = await mint_button_token(ref, "approve", chat_id)
        try:
            reject = await mint_button_token(ref, "reject", chat_id)
        except (TelegramCallbackError,
                human_confirmation.WorkflowError):
            reject = None
        if reject is None:
            keyboard = {"inline_keyboard": [
                [{"text": "Approve",
                    "callback_data": approve["token"]}]]}
        else:
            keyboard = review_keyboard(
                approve["token"], reject["token"])
        body = row.get("message_text") or ""
        body += "\n\nTap Approve for one-tap confirmation, or reply with " \
            "a REVIEW command carrying the reference above."
        if len(body) > 4000:
            body = body[:4000]
        try:
            sent = await outbound_telegram.send_message(
                chat_id, body, reply_markup=keyboard)
        except outbound_telegram.OutboundUnavailable as error:
            await _mark_outbound(client, row["id"], "failed",
                {"failure_reason": _failure_note(error)})
            return "failed"
        await _mark_outbound(client, row["id"], "sent",
            {"sent_at": datetime.now(timezone.utc).isoformat(),
                "provider_message_id": sent.get("provider_message_id")})
        return "sent"
    except (TelegramAdapterError, TelegramCallbackError,
            human_confirmation.WorkflowError) as error:
        try:
            await _mark_outbound(client, row["id"], "failed",
                {"failure_reason": _failure_note(error)})
        except human_confirmation.WorkflowDatabaseError:
            pass
        return "failed"


async def sync_submission_reviews(submission_id, request_key):
    """Operator/scheduler entry point: queues Telegram review requests
    for one draft submission, then dispatches its queued notifications.
    Best-effort per stage; the draft and the WhatsApp flow are never
    affected by a Telegram failure."""
    summary = {"queue_status": None, "dispatch": None, "error": None}
    try:
        queued = await queue_telegram_reviews(submission_id, request_key)
    except (TelegramAdapterError,
            human_confirmation.WorkflowError) as error:
        summary["error"] = "%s: refused" % type(error).__name__
        return summary
    summary["queue_status"] = queued.get("status")
    if queued.get("status") in ("queued", "already_queued"):
        summary["dispatch"] = await dispatch_queued(
            review_ref=queued.get("review_ref"))
    return summary
