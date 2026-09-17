"""Outbound dispatch worker: claims queued biz_outbound_messages rows and
sends them through notifier/outbound_whatsapp, recording retry state on
failure. NOT invoked automatically by anything in this codebase -- suitable
for a future protected internal endpoint or scheduled invocation (Stage
007C scope is the callable worker itself, not wiring it in). No production
message is sent by running this module's tests: every Meta call notifier
makes is mocked.
"""
from supabase_backend import rest_get, rest_patch
import notifier
import retry_engine

BATCH_LIMIT = 20


async def _claim(message_id):
    """Atomic-enough claim via a conditional UPDATE ... WHERE status='queued'.
    Postgres serializes the underlying row UPDATE, so a second concurrent
    claim attempt on the same row matches zero rows and safely no-ops --
    this is what prevents a message from ever being sent twice."""
    response = await rest_patch("/rest/v1/biz_outbound_messages",
        {"id": "eq." + message_id, "status": "eq.queued"}, {"status": "sending"},
        prefer="return=representation")
    rows = response.json()
    return rows[0] if rows else None


async def dispatch_pending(limit=BATCH_LIMIT):
    rows = await rest_get("/rest/v1/biz_outbound_messages", {
        "status": "eq.queued", "order": "queued_at.asc", "limit": str(limit)})
    summary = {"scanned": 0, "sent": 0, "failed": 0, "claim_conflicts": 0}
    for row in rows:
        summary["scanned"] += 1
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
        else:
            await retry_engine.record_failure(claimed["tenant_id"], "outbound_message", claimed["id"],
                result.get("failure_reason") or "send failed",
                business_id=claimed.get("business_id"), branch_id=claimed.get("branch_id"))
            summary["failed"] += 1
    return summary
