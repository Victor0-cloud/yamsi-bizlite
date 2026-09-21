"""Outbound dispatch worker: claims queued biz_outbound_messages rows and
sends them through notifier/outbound_whatsapp, recording retry state on
failure. Production entry points: the WhatsApp webhook background trigger
(one bounded pass per delivery, errors contained) and the owner-gated
POST /internal/dispatch-outbound route for backlog and recovery (see app.py).
No polling loop or scheduler exists. No production message is sent by
running this module's tests: every Meta call notifier makes is mocked.

Retry discipline (bounded, no loops):

- Rows with a future next_attempt_at are skipped until due.
- A failure the client classified retryable (timeouts, 429/5xx) returns
  the row to 'queued' with attempt_count+1 and a backoff deadline, until
  OUTBOUND_MAX_ATTEMPTS attempts are spent -- then it fails terminally.
- Permanent failures (invalid recipient/payload, Meta 4xx) and unexpected
  bugs fail terminally on the spot, exactly as before.
- Backoff is deterministic: retry_delay_minutes(attempt) doubles from
  5 minutes capped at 4 hours. No fabricated per-row schedule beyond
  this bound; attempt history stays auditable on the row.
- The row's own provider_account snapshot is never re-derived or
  switched at any point (see the send-uses-row-account test).

Crash recovery lives in recover_stale_claims(), which runs the
service-role-only amose_reclaim_stale_outbound RPC: 'sending' rows whose
claimed_at lease expired return to 'queued' transactionally. It runs only
through the owner-gated POST /internal/recover-stale-outbound route --
never inside dispatch_pending, so a dispatch pass stays bounded and a
Render restart simply leaves the next pass (or recovery call) to pick
up the work.
"""
from datetime import datetime, timedelta, timezone

import httpx

from supabase_backend import credentials, DatabaseUnavailable, rest_get, rest_patch
import notifier
import retry_engine

BATCH_LIMIT = 20
MAX_LIMIT = 100

# Bounded retry policy for the worker itself.
OUTBOUND_MAX_ATTEMPTS = 5
RETRY_BASE_DELAY_MINUTES = 5
RETRY_MAX_DELAY_MINUTES = 240
DEFAULT_STALE_SECONDS = 1800
RECLAIM_RPC = "amose_reclaim_stale_outbound"
RECLAIM_ALLOWLIST = frozenset({RECLAIM_RPC})


def parse_limit(value):
    """Bounds an owner-supplied dispatch batch size to 1..MAX_LIMIT.
    Pure helper (no I/O) so the validation is unit-testable on its own;
    missing or non-numeric input falls back to BATCH_LIMIT."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return BATCH_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def retry_delay_minutes(attempt):
    """Deterministic backoff for a 1-based retry attempt number: 5, 10,
    20, ... minutes capped at 4 hours. Pure helper, unit-tested."""
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 1
    if attempt < 1:
        attempt = 1
    return min(RETRY_MAX_DELAY_MINUTES,
        RETRY_BASE_DELAY_MINUTES * (2 ** (attempt - 1)))


def _now():
    return datetime.now(timezone.utc)


def _attempt_count(row):
    count = row.get("attempt_count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _is_due(row, now):
    """A row without a future next_attempt_at is due. Unparseable values
    fail open to due (the row was explicitly queued; only a valid future
    deadline defers it)."""
    raw = row.get("next_attempt_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return True
    if not isinstance(raw, str):
        return True
    try:
        return datetime.fromisoformat(raw) <= now
    except ValueError:
        return True


async def _claim(message_id):
    """Atomic-enough claim via a conditional UPDATE ... WHERE status='queued'.
    Postgres serializes the underlying row UPDATE, so a second concurrent
    claim attempt on the same row matches zero rows and safely no-ops --
    this is what prevents a message from ever being sent twice. Stamps
    claimed_at so a crashed pass leaves a recoverable lease instead of a
    stuck row. A refused claim (conflict, or the provider-account guard
    rejecting a disabled account) returns None and counts as a conflict."""
    try:
        response = await rest_patch("/rest/v1/biz_outbound_messages",
            {"id": "eq." + message_id, "status": "eq.queued"},
            {"status": "sending", "claimed_at": _now().isoformat()},
            prefer="return=representation")
    except DatabaseUnavailable:
        return None
    rows = response.json()
    return rows[0] if rows else None


async def dispatch_pending(limit=BATCH_LIMIT, now=None):
    rows = await rest_get("/rest/v1/biz_outbound_messages", {
        "status": "eq.queued", "order": "queued_at.asc", "limit": str(limit)})
    now = now or _now()
    summary = {"scanned": 0, "sent": 0, "failed": 0, "claim_conflicts": 0}
    for row in rows:
        summary["scanned"] += 1
        if not _is_due(row, now):
            continue
        claimed = await _claim(row["id"])
        if claimed is None:
            summary["claim_conflicts"] += 1
            continue
        try:
            result = await notifier.dispatch_queued_message(claimed, claimed.get("provider_account"))
        except Exception as error:
            await retry_engine.record_failure(claimed["tenant_id"], "outbound_message", claimed["id"], error,
                business_id=claimed.get("business_id"), branch_id=claimed.get("branch_id"))
            await rest_patch("/rest/v1/biz_outbound_messages", {"id": "eq." + claimed["id"]},
                {"status": "failed", "failure_reason": str(error)[:500]})
            summary["failed"] += 1
            continue
        if result.get("status") == "sent":
            await retry_engine.record_success(claimed["tenant_id"], "outbound_message", claimed["id"])
            summary["sent"] += 1
            continue
        attempts = _attempt_count(claimed) + 1
        if result.get("retryable") and attempts < OUTBOUND_MAX_ATTEMPTS:
            delay = retry_delay_minutes(attempts)
            await rest_patch("/rest/v1/biz_outbound_messages", {"id": "eq." + claimed["id"]},
                {"status": "queued", "attempt_count": attempts,
                    "next_attempt_at": (now + timedelta(minutes=delay)).isoformat(),
                    "failure_reason": (result.get("failure_reason") or "send failed")[:500]})
            await retry_engine.record_failure(claimed["tenant_id"], "outbound_message", claimed["id"],
                result.get("failure_reason") or "send failed",
                business_id=claimed.get("business_id"), branch_id=claimed.get("branch_id"))
            continue
        await retry_engine.record_failure(claimed["tenant_id"], "outbound_message", claimed["id"],
            result.get("failure_reason") or "send failed",
            business_id=claimed.get("business_id"), branch_id=claimed.get("branch_id"))
        await rest_patch("/rest/v1/biz_outbound_messages", {"id": "eq." + claimed["id"]},
            {"status": "failed", "attempt_count": attempts,
                "failure_reason": (result.get("failure_reason") or "send failed")[:500]})
        summary["failed"] += 1
    return summary


async def recover_stale_claims(stale_seconds=DEFAULT_STALE_SECONDS, limit=BATCH_LIMIT):
    """Runs the controlled stale-claim recovery RPC once and returns its
    result. Owner-gated callers only; never called from dispatch_pending,
    so recovery stays an explicit bounded action. Raises
    OutboundRecoveryError when the database is unreachable or the RPC
    refuses."""
    try:
        stale = int(stale_seconds)
    except (TypeError, ValueError):
        raise OutboundRecoveryError("stale_seconds must be an integer")
    if stale < 60 or stale > 86400:
        raise OutboundRecoveryError("stale_seconds must be 60..86400")
    if RECLAIM_RPC not in RECLAIM_ALLOWLIST:
        raise OutboundRecoveryError("Refusing to call non-allowlisted function")
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise OutboundRecoveryError("Supabase is not configured: " + str(exc)) from None
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=20, follow_redirects=False) as client:
            response = await client.post("/rest/v1/rpc/" + RECLAIM_RPC,
                json={"p_stale_seconds": stale, "p_limit": parse_limit(limit)})
    except httpx.HTTPError:
        raise OutboundRecoveryError("Recovery RPC unreachable") from None
    if response.status_code not in (200, 201):
        raise OutboundRecoveryError("Recovery RPC refused the request")
    try:
        result = response.json()
    except ValueError:
        raise OutboundRecoveryError("Recovery RPC returned invalid JSON") from None
    if not isinstance(result, dict) or result.get("status") != "ok" \
            or not isinstance(result.get("reclaimed"), int):
        raise OutboundRecoveryError("Recovery RPC returned an unexpected result")
    return result


class OutboundRecoveryError(Exception):
    """Stale-claim recovery was refused or failed; nothing was half-moved
    (the RPC is transactional, so a failure means a full rollback)."""
