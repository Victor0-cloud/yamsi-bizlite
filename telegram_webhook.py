"""Telegram webhook: authenticated ingestion only.

POST /webhooks/telegram accepts Telegram Bot API updates. Authenticity is
established FIRST by the X-Telegram-Bot-Api-Secret-Token header against
TELEGRAM_WEBHOOK_SECRET -- a missing or incorrect secret is rejected before
the body is parsed or processed. Valid updates are stored durably in
biz_message_inbox (provider='telegram', deduplicated transactionally on
(update_id) via the inbox unique constraint) before any further work is
scheduled as a best-effort background task.

No financial posting, no review decisions, and no outgoing messages happen
in this module. WhatsApp handling is untouched, and no WhatsApp credential
is read here.
"""
import json
import os
import secrets

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from supabase_backend import credentials, DatabaseUnavailable
from telegram_adapter import (
    BOT_ACCOUNT,
    MAX_BYTES,
    PROVIDER,
    parse_update,
    process_stored_update,
    provider_event_id,
)

router = APIRouter()

SECRET_HEADER = "x-telegram-bot-api-secret-token"


def _check_secret(request):
    configured = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not configured:
        raise HTTPException(
            503, "Webhook authenticity validation is not configured")
    supplied = request.headers.get(SECRET_HEADER, "")
    if not secrets.compare_digest(supplied.encode(), configured.encode()):
        raise HTTPException(403, "Invalid webhook secret")


async def save_update(update_id, update):
    """Durably stores one validated update. Returns the inbox row id, or
    None when this update_id was already stored (transactional duplicate:
    the unique constraint plus ignore-duplicates turns a retry into a
    no-op, so replays never schedule a second processing pass)."""
    row = {"provider": PROVIDER, "provider_account": BOT_ACCOUNT,
        "provider_event_id": provider_event_id(update_id),
        "payload": {"kind": "telegram_update", "update": update}}
    try:
        url, key = credentials()
        headers = {"apikey": key,
            "Prefer": "resolution=ignore-duplicates,return=representation"}
        if key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + key
        async with httpx.AsyncClient(timeout=8,
                follow_redirects=False) as client:
            response = await client.post(url + "/rest/v1/biz_message_inbox",
                params={"on_conflict":
                    "provider,provider_account,provider_event_id"},
                headers=headers, json=[row])
        if response.status_code not in (200, 201):
            raise DatabaseUnavailable("Inbox unavailable")
        stored = response.json()
    except (httpx.HTTPError, DatabaseUnavailable, ValueError):
        raise HTTPException(
            503, "Inbox unavailable; retry delivery") from None
    if not stored:
        return None
    try:
        return stored[0]["id"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(
            503, "Inbox unavailable; retry delivery") from None


async def _trigger_processing(update, inbox_id):
    """Best-effort, fire-and-forget: runs strictly AFTER the durable inbox
    insert has already succeeded, so it never delays or risks the
    webhook's response to Telegram. Any failure here is contained to the
    row (status='failed' with a generic note, or consumed as processed
    for terminal refusals) and never raises."""
    try:
        await process_stored_update(update, inbox_id)
    except Exception:
        pass


@router.post("/webhooks/telegram")
async def receive(request: Request, background_tasks: BackgroundTasks):
    # Secret first: reject unauthenticated callers before spending any
    # effort on the body beyond the bounded read below.
    _check_secret(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "Payload too large")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(400, "Malformed webhook payload") from None
    from telegram_adapter import TelegramAdapterError
    try:
        update = parse_update(payload)
    except TelegramAdapterError:
        raise HTTPException(400, "Malformed webhook payload") from None
    inbox_id = await save_update(update["update_id"], payload)
    if inbox_id is None:
        return {"received": True, "duplicate": True}
    # Durable insertion above has already succeeded at this point; only
    # now is any further (non-durable, best-effort) work scheduled.
    background_tasks.add_task(_trigger_processing, update, inbox_id)
    return {"received": True}
