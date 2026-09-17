"""Outbound WhatsApp Cloud API client.

WHATSAPP_ACCESS_TOKEN is read from the environment only, at call time, and
is never included in any exception message, log line, return value, or
database row. Nothing in this module sends automatically -- send_text_message
must be explicitly called by a caller that has already decided to send (see
notifier.py, which queues messages but does not call this on its own).
"""
import os
import httpx

GRAPH_API_VERSION = "v21.0"


class OutboundUnavailable(Exception):
    pass


def _access_token():
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token:
        raise OutboundUnavailable("WhatsApp outbound sending is not configured")
    return token


async def send_text_message(phone_number_id, to, body):
    """phone_number_id: the business's own WhatsApp number id (reused from
    the inbound webhook's metadata.phone_number_id -- see notifier.py --
    never a new hard-coded value). to: recipient's WhatsApp id/phone in the
    same digits-only international format the webhook already uses."""
    token = _access_token()
    url = "https://graph.facebook.com/%s/%s/messages" % (GRAPH_API_VERSION, phone_number_id)
    headers = {"Authorization": "Bearer " + token}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError:
        raise OutboundUnavailable("WhatsApp send request failed") from None
    if response.status_code != 200:
        raise OutboundUnavailable("WhatsApp API returned status %d" % response.status_code)
    try:
        data = response.json()
        message_id = (data.get("messages") or [{}])[0].get("id")
    except (ValueError, KeyError, IndexError, TypeError):
        message_id = None
    return {"provider_message_id": message_id}
