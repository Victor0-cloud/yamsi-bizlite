"""Outbound-message queue: the ONLY place rule_engine touches when it wants
YAMSI to eventually tell a human something. queue_message() only ever
records intent (a biz_outbound_messages row) -- it never calls the WhatsApp
API itself. dispatch_queued_message() does the real send, but nothing in
this codebase calls it automatically yet: real sending stays an explicit,
separately-approved action (see Stage 007B report, item N).

If the recipient has no confirmed biz_sender_identities row for this
provider, the message is recorded as status='skipped_no_identity' -- never
guessed, never silently dropped.
"""
from datetime import datetime, timezone
from supabase_backend import rest_get, rest_post, rest_patch
import outbound_whatsapp


async def queue_message(tenant_id, business_id, branch_id, recipient_employee_id, related_task_id,
        message_type, message_text, provider="whatsapp", provider_account=None):
    """Idempotent: at most one queued message per (related_task_id, message_type).
    provider_account: the business's own WhatsApp phone_number_id, snapshotted
    at queue time (reused from the triggering inbox row) so the dispatch
    worker never has to re-derive it later."""
    idempotency_key = "%s:%s" % (related_task_id, message_type)
    existing = await rest_get("/rest/v1/biz_outbound_messages", {"idempotency_key": "eq." + idempotency_key})
    if existing:
        return existing[0]
    sender_rows = await rest_get("/rest/v1/biz_sender_identities", {
        "tenant_id": "eq." + tenant_id, "employee_id": "eq." + recipient_employee_id,
        "provider": "eq." + provider, "select": "provider_sender"})
    provider_sender = sender_rows[0]["provider_sender"] if sender_rows else None
    row = {"tenant_id": tenant_id, "business_id": business_id, "branch_id": branch_id,
        "recipient_employee_id": recipient_employee_id, "provider": provider,
        "provider_sender": provider_sender, "provider_account": provider_account,
        "related_task_id": related_task_id, "message_type": message_type, "message_text": message_text,
        "status": "queued" if provider_sender else "skipped_no_identity",
        "idempotency_key": idempotency_key}
    await rest_post("/rest/v1/biz_outbound_messages", [row], prefer="return=minimal")
    rows = await rest_get("/rest/v1/biz_outbound_messages", {"idempotency_key": "eq." + idempotency_key})
    return rows[0] if rows else None


async def dispatch_queued_message(message_row, phone_number_id):
    """Performs the real send for a single already-queued (or already-
    claimed, status='sending' -- see outbound_dispatch_worker) message.
    Reached only through the dispatch worker (webhook background trigger
    or owner endpoint), never directly from ingestion. Uses exactly the
    row's own provider_account snapshot -- never a re-derived value.

    Failure results carry retryable=True only for errors the client
    classified retryable (timeouts, 429/5xx); every other failure,
    including unclassified legacy OutboundUnavailable errors, reports
    retryable=False so the worker never retries a final refusal."""
    if message_row["status"] not in ("queued", "sending") or not message_row.get("provider_sender"):
        return message_row
    try:
        result = await outbound_whatsapp.send_text_message(
            phone_number_id, message_row["provider_sender"], message_row["message_text"])
    except outbound_whatsapp.OutboundUnavailable as error:
        failure_reason = str(error)
        await rest_patch("/rest/v1/biz_outbound_messages", {"id": "eq." + message_row["id"]},
            {"status": "failed", "failure_reason": failure_reason})
        return {**message_row, "status": "failed", "failure_reason": failure_reason,
            "retryable": bool(getattr(error, "retryable", False))}
    sent_at = datetime.now(timezone.utc).isoformat()
    await rest_patch("/rest/v1/biz_outbound_messages", {"id": "eq." + message_row["id"]},
        {"status": "sent", "sent_at": sent_at, "provider_message_id": result.get("provider_message_id")})
    return {**message_row, "status": "sent", "sent_at": sent_at, "provider_message_id": result.get("provider_message_id")}
