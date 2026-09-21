"""Outbound WhatsApp Cloud API client (hardened for live use).

WHATSAPP_ACCESS_TOKEN is read from the environment only, at call time, and
is never included in any exception message, log line, return value, or
database row. Nothing in this module sends automatically -- send_text_message
must be explicitly called by a caller that has already decided to send (see
notifier.py, which queues messages but does not call this on its own).

Live-safety properties:

- The Graph API version is explicit (GRAPH_API_VERSION) and validated
  against SUPPORTED_GRAPH_API_VERSIONS; WHATSAPP_GRAPH_API_VERSION may
  select another supported version but never an unlisted one.
- The business phone_number_id must be a non-blank string. Authorization
  of that number for the message's scope is enforced database-side (see
  biz_provider_accounts and the outbound claim guard); this client never
  invents or substitutes a number.
- Recipients are validated conservatively (digits, 5..15 chars after
  stripping one leading '+'); no country code is ever guessed.
- Text must be a non-empty string within WhatsApp's 4096-character limit.
- Bounded timeouts, no redirects, exactly one POST per call.
- Meta responses are parsed conservatively: only a
  messages[0].id string counts as acceptance (returned as
  provider_message_id so delivery statuses correlate later).
- Errors are sanitized: token, request body, and Meta response bodies
  never appear in exceptions. Failures classify as retryable (timeouts,
  429, 5xx) or permanent (validation, 4xx, unparseable acceptance) via
  the retryable attribute, so the dispatch worker can bound retries
  without ever re-sending on a permanent refusal.
"""

import os
import re
import httpx

GRAPH_API_VERSION = "v21.0"
SUPPORTED_GRAPH_API_VERSIONS = frozenset({"v21.0"})

MAX_TEXT_LENGTH = 4096
_RECIPIENT_PATTERN = re.compile(r"^[0-9]{5,15}$")

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

# Optional operator override, validated by production_readiness: a present,
# in-range OUTBOUND_HTTP_TIMEOUT_SECONDS scales the read/write legs only.
# Unset or unusable values keep the compiled default above, so existing
# behavior (and every existing test) is untouched.
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 120.0


def _timeout():
    raw = os.environ.get("OUTBOUND_HTTP_TIMEOUT_SECONDS", "")
    if isinstance(raw, str) and raw.strip():
        try:
            seconds = float(raw.strip())
        except ValueError:
            seconds = None
        if seconds is not None \
                and _MIN_TIMEOUT_SECONDS <= seconds <= _MAX_TIMEOUT_SECONDS:
            return httpx.Timeout(connect=5.0, read=seconds,
                write=seconds, pool=5.0)
    return _TIMEOUT


class OutboundUnavailable(Exception):
    """Base class: the send did not happen. See retryable for whether a
    later attempt could succeed (True) or the refusal is final (False)."""
    retryable = False


class RetryableOutboundError(OutboundUnavailable):
    """Timeouts and Meta 429/5xx: safe to retry within bounded policy."""
    retryable = True


class PermanentOutboundError(OutboundUnavailable):
    """Validation failures and Meta 4xx: retrying cannot succeed."""
    retryable = False


class OutboundNotConfigured(OutboundUnavailable):
    """Missing token or unsupported API version: fail closed."""
    retryable = False


def _api_version():
    configured = os.environ.get("WHATSAPP_GRAPH_API_VERSION",
        GRAPH_API_VERSION)
    if not isinstance(configured, str) \
            or configured.strip() not in SUPPORTED_GRAPH_API_VERSIONS:
        raise OutboundNotConfigured(
            "WhatsApp Graph API version is not supported")
    return configured.strip()


def _access_token():
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not isinstance(token, str) or not token.strip():
        raise OutboundNotConfigured(
            "WhatsApp outbound sending is not configured")
    return token


def _normalize_recipient(to):
    if not isinstance(to, str):
        raise PermanentOutboundError("failure=invalid_recipient")
    digits = to.strip()
    if digits.startswith("+"):
        digits = digits[1:]
    if not _RECIPIENT_PATTERN.match(digits):
        raise PermanentOutboundError("failure=invalid_recipient")
    return digits


def _validate_body(body):
    if not isinstance(body, str) or not body.strip():
        raise PermanentOutboundError("failure=invalid_message")
    if len(body) > MAX_TEXT_LENGTH:
        raise PermanentOutboundError("failure=message_too_long")
    return body


def _classify_http_status(status_code, error_code=None):
    """Sanitized failure code from status + optional Meta numeric error
    code. Ints only -- response bodies never flow into errors."""
    if status_code == 429 or (isinstance(status_code, int)
            and 500 <= status_code <= 599):
        return "failure=meta_retryable status=%d" % status_code
    code = "failure=meta_refused status=%d" % status_code
    if isinstance(error_code, int):
        code += " code=%d" % error_code
    return code


def _meta_error_code(data):
    try:
        code = (data.get("error") or {}).get("code")
    except AttributeError:
        return None
    return code if isinstance(code, int) else None


async def send_text_message(phone_number_id, to, body):
    """Sends one text message. Returns {"provider_message_id": ...} on
    acceptance. Raises RetryableOutboundError (safe to retry bounded),
    PermanentOutboundError (do not retry), or OutboundNotConfigured
    (fail closed). No real network call happens without an explicit
    caller; tests always mock httpx."""
    if not isinstance(phone_number_id, str) or not phone_number_id.strip():
        raise PermanentOutboundError("failure=invalid_provider_account")
    recipient = _normalize_recipient(to)
    text = _validate_body(body)
    version = _api_version()
    token = _access_token()
    url = "https://graph.facebook.com/%s/%s/messages" % (
        version, phone_number_id.strip())
    headers = {"Authorization": "Bearer " + token}
    payload = {"messaging_product": "whatsapp", "to": recipient,
        "type": "text", "text": {"body": text}}
    try:
        async with httpx.AsyncClient(timeout=_timeout(),
                follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise RetryableOutboundError("failure=timeout") from None
    except httpx.HTTPError:
        raise RetryableOutboundError("failure=transport") from None
    status_code = response.status_code
    if status_code != 200:
        code = _classify_http_status(status_code)
        if status_code == 429 or 500 <= status_code <= 599:
            raise RetryableOutboundError(code) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            code = _classify_http_status(status_code,
                _meta_error_code(data))
        raise PermanentOutboundError(code) from None
    try:
        data = response.json()
        message_id = (data.get("messages") or [{}])[0].get("id")
    except (ValueError, AttributeError, IndexError, TypeError):
        message_id = None
    if not isinstance(message_id, str) or not message_id:
        raise PermanentOutboundError("failure=meta_unexpected_response")
    return {"provider_message_id": message_id}
