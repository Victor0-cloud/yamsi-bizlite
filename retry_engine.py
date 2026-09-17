"""Retry/dead-letter tracking for inbox processing, outbound WhatsApp
delivery, and media retrieval failures (public.biz_retry_state).

Mirrors rule_engine's reminder-policy pattern exactly: automatic scheduling
only ever uses an owner-confirmed public.biz_setting_versions policy
(key='processing_retry_policy', scoped per tenant/business/branch). With no
configured (or disabled/malformed) policy, a retry_state row is still
created/updated -- so the failure is tracked and visible -- but
next_attempt_at/max_attempts stay NULL and the row stays state='pending':
automatic retry scheduling remains disabled. Never a fabricated cadence.
"""
from datetime import datetime, timedelta, timezone
from supabase_backend import rest_get, rest_post, rest_patch

RETRY_POLICY_KEY = "processing_retry_policy"
SUBJECT_TYPES = ("inbox_processing", "outbound_message", "media_retrieval")
RETRY_POLICY_FIELDS = ("enabled", "max_attempts", "retry_interval_minutes")

_SUBJECT_COLUMN = {
    "inbox_processing": "inbox_id",
    "outbound_message": "outbound_message_id",
    "media_retrieval": "evidence_id",
}


def validate_retry_policy(value):
    """Same gate style as rule_engine.validate_reminder_policy: a missing
    or malformed policy is never partially trusted."""
    if not isinstance(value, dict):
        return False, "Policy must be an object"
    if not isinstance(value.get("enabled"), bool):
        return False, "Policy must include a boolean 'enabled' field"
    for field in ("max_attempts", "retry_interval_minutes"):
        if field in value and value[field] is not None and not isinstance(value[field], (int, float)):
            return False, "%s must be numeric" % field
    return True, None


async def get_retry_policy(tenant_id, business_id, branch_id):
    """Owner-confirmed, validated, enabled policy, or None. Retry policy is
    scoped per business/branch (like the reminder policy) -- if the
    business/branch scope isn't known yet (e.g. a failure before sender
    identity resolved), there is nothing to look up and scheduling simply
    stays disabled for that failure."""
    if not business_id or not branch_id:
        return None
    rows = await rest_get("/rest/v1/biz_setting_versions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "key": "eq." + RETRY_POLICY_KEY, "order": "effective_from.desc", "limit": "1", "select": "value"})
    if not rows:
        return None
    value = rows[0]["value"]
    valid, _reason = validate_retry_policy(value)
    if not valid or not value.get("enabled"):
        return None
    return value


async def get_or_create_retry_state(tenant_id, subject_type, subject_id):
    """Idempotent: exactly one retry_state row per subject (enforced by the
    unique constraint on each typed FK column)."""
    column = _SUBJECT_COLUMN[subject_type]
    existing = await rest_get("/rest/v1/biz_retry_state", {column: "eq." + subject_id})
    if existing:
        return existing[0]
    row = {"tenant_id": tenant_id, "subject_type": subject_type, column: subject_id}
    await rest_post("/rest/v1/biz_retry_state", [row], prefer="return=minimal")
    rows = await rest_get("/rest/v1/biz_retry_state", {column: "eq." + subject_id})
    return rows[0] if rows else None


async def record_failure(tenant_id, subject_type, subject_id, error, *, business_id=None, branch_id=None, now=None):
    """Records a failed attempt. Only schedules a next attempt (or declares
    dead_letter) when an owner-confirmed policy says so; otherwise the row
    stays 'pending' -- tracked, but with no invented schedule."""
    now = now or datetime.now(timezone.utc)
    state = await get_or_create_retry_state(tenant_id, subject_type, subject_id)
    attempt_count = state["attempt_count"] + 1
    policy = await get_retry_policy(tenant_id, business_id, branch_id)
    body = {"attempt_count": attempt_count, "last_attempt_at": now.isoformat(), "last_error": str(error)[:500]}
    if policy:
        max_attempts = policy.get("max_attempts")
        interval_minutes = policy.get("retry_interval_minutes")
        if max_attempts is not None and attempt_count >= max_attempts:
            body["state"] = "dead_letter"
            body["next_attempt_at"] = None
        else:
            body["state"] = "scheduled"
            body["max_attempts"] = max_attempts
            if interval_minutes is not None:
                body["next_attempt_at"] = (now + timedelta(minutes=interval_minutes)).isoformat()
    else:
        body["state"] = "pending"
        body["next_attempt_at"] = None
    await rest_patch("/rest/v1/biz_retry_state", {"id": "eq." + state["id"]}, body)
    return {**state, **body, "policy_configured": policy is not None}


async def record_success(tenant_id, subject_type, subject_id):
    """Marks any existing retry_state row for this subject 'resolved'. A
    no-op if the subject never failed (no row exists)."""
    column = _SUBJECT_COLUMN[subject_type]
    existing = await rest_get("/rest/v1/biz_retry_state", {column: "eq." + subject_id})
    if not existing:
        return None
    await rest_patch("/rest/v1/biz_retry_state", {"id": "eq." + existing[0]["id"]}, {"state": "resolved"})
    return existing[0]["id"]
