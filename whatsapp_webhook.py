"""Authenticated ingestion only. No financial posting or outgoing messages."""
import hashlib
import hmac
import json
import os
import secrets
import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from supabase_backend import credentials, DatabaseUnavailable

router = APIRouter()
MAX_BYTES = 1024 * 1024

@router.get("/webhooks/whatsapp")
async def verify(request: Request):
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Webhook verification is not configured")
    params = request.query_params
    supplied = params.get("hub.verify_token", "")
    if params.get("hub.mode") != "subscribe" or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(403, "Verification denied")
    challenge = params.get("hub.challenge")
    if not challenge or len(challenge) > 1024:
        raise HTTPException(400, "Missing or invalid challenge")
    return PlainTextResponse(challenge)

def extract_events(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected object")
    if payload.get("object") != "whatsapp_business_account":
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise ValueError("Invalid entries")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
            raise ValueError("Invalid entry")
        for change in entry["changes"]:
            if not isinstance(change, dict):
                raise ValueError("Invalid change")
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                raise ValueError("Invalid value")
            metadata = value.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("Invalid metadata")
            account = metadata.get("phone_number_id")
            for group, kind in (("messages", "message"), ("statuses", "status")):
                items = value.get(group, [])
                if not isinstance(items, list):
                    raise ValueError("Invalid event list")
                for event in items:
                    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
                        raise ValueError("Invalid event identifier")
                    if not isinstance(account, str) or not account:
                        raise ValueError("Missing phone number identity")
                    if kind == "message":
                        identity = "message:" + event["id"]
                    else:
                        if not isinstance(event.get("status"), str) or not isinstance(event.get("timestamp"), str):
                            raise ValueError("Invalid status")
                        identity = "status:" + hashlib.sha256(json.dumps(
                            [event["id"], event["status"], event["timestamp"]], separators=(",", ":")).encode()).hexdigest()
                    result.append({"provider":"whatsapp", "provider_account":account,
                        "provider_event_id":identity, "payload":{"kind":kind,"event":event}})
    # Collapse duplicates within one batch as well as across webhook requests.
    return list({(r["provider_account"],r["provider_event_id"]):r for r in result}.values())

async def save_events(rows):
    try:
        url, key = credentials()
        headers = {"apikey":key, "Prefer":"resolution=ignore-duplicates,return=minimal"}
        if key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + key
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            response = await client.post(url + "/rest/v1/biz_message_inbox",
                params={"on_conflict":"provider,provider_account,provider_event_id"},
                headers=headers, json=rows)
        if response.status_code not in (200, 201, 204):
            raise DatabaseUnavailable("Inbox unavailable")
    except (httpx.HTTPError, DatabaseUnavailable):
        raise HTTPException(503, "Inbox unavailable; retry delivery") from None

AUTO_DISPATCH_LIMIT = 10


async def _trigger_dispatch(limit=AUTO_DISPATCH_LIMIT):
    """Best-effort bounded dispatch pass: claims queued outbound rows once
    each and sends them, recording retry state on failure. All errors are
    swallowed -- dispatch trouble is recorded on the rows themselves (see
    outbound_dispatch_worker.dispatch_pending) and must never surface here,
    let alone fail the webhook acknowledgement that already went out."""
    from outbound_dispatch_worker import dispatch_pending
    try:
        await dispatch_pending(limit=limit)
    except Exception:
        pass


async def _trigger_processing():
    """Best-effort, fire-and-forget: runs strictly AFTER the durable inbox
    insert has already succeeded, scheduled as a FastAPI background task so
    it never delays or risks the webhook's response to Meta. Any failure
    here (including Supabase being unreachable) is swallowed -- the row
    stays at status='received' (or gets marked 'failed' with an error, see
    message_processor.process_inbox_batch) and is picked up by a later
    call, whether that's the next webhook delivery's background task or a
    manual POST /internal/process-whatsapp-inbox. Nothing performed here can
    ever cause a message to be lost or duplicated. After processing, one
    bounded dispatch pass is attempted the same contained way, so replies
    queued by this delivery (e.g. branch_clarification) are sent without
    waiting for an operator; POST /internal/dispatch-outbound remains for
    backlog and recovery."""
    from message_processor import process_inbox_batch
    try:
        await process_inbox_batch()
    except Exception:
        pass
    await _trigger_dispatch()

@router.post("/webhooks/whatsapp")
async def receive(request: Request, background_tasks: BackgroundTasks):
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        raise HTTPException(503, "Webhook authenticity validation is not configured")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "Payload too large")
    expected = "sha256=" + hmac.new(secret.encode(), bytes(raw), hashlib.sha256).hexdigest()
    supplied = request.headers.get("x-hub-signature-256", "")
    if not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(403, "Invalid signature")
    try:
        payload = json.loads(raw)
        rows = extract_events(payload)
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(400, "Malformed webhook payload") from None
    if rows:
        await save_events(rows)
        # Durable insertion above has already succeeded at this point; only
        # now is any further (non-durable, best-effort) work scheduled.
        background_tasks.add_task(_trigger_processing)
    return {"received":True,"events":len(rows)}
