"""Outbound Telegram Bot API client (review-backup channel only).

TELEGRAM_BOT_TOKEN is read from the environment only, at call time, and is
never included in any exception message, log line, return value, or
database row. Nothing in this module sends automatically -- the send/answer
helpers must be explicitly called by a caller that has already decided to
send (see telegram_adapter.py). This module performs zero database writes
and never touches WhatsApp credentials.
"""
import os

import httpx

API_BASE = "https://api.telegram.org"


class OutboundUnavailable(Exception):
    pass


def _bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise OutboundUnavailable("Telegram outbound sending is not configured")
    return token


def _api_url(method):
    # The token travels in the URL path per the Bot API contract; it is
    # never copied into errors, logs, or return values below.
    return "%s/bot%s/%s" % (API_BASE, _bot_token(), method)


def _raise_for_status(response, method):
    if response.status_code != 200:
        raise OutboundUnavailable("Telegram API call failed: " + method)
    try:
        body = response.json()
    except ValueError:
        raise OutboundUnavailable("Telegram API returned invalid JSON") from None
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise OutboundUnavailable("Telegram API refused the request: " + method)
    return body.get("result")


async def send_message(chat_id, text, reply_markup=None):
    """Sends a Telegram message. chat_id is the numeric chat id resolved
    server-side (never a user-supplied username). reply_markup, when given,
    is a server-built inline keyboard (opaque tokens only)."""
    if not isinstance(chat_id, str) or not chat_id:
        raise OutboundUnavailable("A chat recipient is required")
    if not isinstance(text, str) or not text:
        raise OutboundUnavailable("Message text is required")
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    # Resolves (and validates) the token BEFORE any client exists, so an
    # unconfigured bot never opens a connection.
    url = _api_url("sendMessage")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError:
        raise OutboundUnavailable("Telegram send request failed") from None
    result = _raise_for_status(response, "sendMessage")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return {"provider_message_id": message_id}


async def answer_callback_query(callback_query_id, text=None):
    """Acknowledges a button press so the client's spinner clears. Best
    effort by contract: failures never raise (callers must not fail a
    review because an acknowledgement could not be delivered)."""
    try:
        url = _api_url("answerCallbackQuery")
        payload = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
        _raise_for_status(response, "answerCallbackQuery")
    except (OutboundUnavailable, httpx.HTTPError):
        pass


async def clear_inline_keyboard(chat_id, message_id):
    """Removes the buttons from a notification after its action was handled,
    so stale buttons cannot confuse reviewers. Best effort: never raises."""
    try:
        url = _api_url("editMessageReplyMarkup")
        payload = {"chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []}}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
        _raise_for_status(response, "editMessageReplyMarkup")
    except (OutboundUnavailable, httpx.HTTPError):
        pass
