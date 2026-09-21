"""Secret-safe structured logging for production operations.

Central rules, applied everywhere this module is used:

- Secret-shaped values (tokens, keys, secrets, authorization headers,
  passwords, database URLs) are never emitted -- the field renders as
  "[REDACTED]".
- Phone numbers render masked: every digit but the final four is
  replaced, so a log line can identify a recipient without exposing
  personally identifiable information.
- Log lines are single-line "event key=value ..." records with stable
  field ordering, so they stay greppable and testable.

Nothing here reads configuration or touches the network; it only
formats values for the standard logging pipeline.
"""

import logging

logger = logging.getLogger("yamsi")

_MASKED_NUMBER = "***"
_REDACTED = "[REDACTED]"

# Field names whose values are phone numbers (or may contain one).
_PHONE_FIELDS = frozenset({
    "to", "from", "recipient", "provider_sender", "sender",
    "phone", "phone_number", "business_number", "staff_number",
})

# Field-name fragments whose values must never be logged.
_SECRET_FRAGMENTS = ("token", "secret", "password", "authorization",
    "apikey", "api_key", "service_key", "service_role", "database_url",
    "webhook_secret", "verify_token")


def mask_phone(value):
    """Masks a phone number, keeping only the final four digits.

    "+12025550123" -> "+*******0123". Non-strings, blanks, and numbers
    with four or fewer digits reveal nothing and render as "***".
    """
    if not isinstance(value, str) or not value.strip():
        return _MASKED_NUMBER
    text = value.strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) <= 4:
        return _MASKED_NUMBER
    prefix = "+" if text.startswith("+") else ""
    return prefix + "*" * (len(digits) - 4) + digits[-4:]


def _safe_value(name, value):
    lowered = name.lower()
    if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
        return _REDACTED
    if lowered in _PHONE_FIELDS:
        return mask_phone(value) if isinstance(value, str) else value
    if isinstance(value, str) and (
            value.startswith("eyJ") or value.startswith("sha256=")):
        return _REDACTED
    return value


def format_event(event, fields):
    """Renders one deterministic log line. Field order is sorted so the
    output is stable across runs and assertable in tests."""
    parts = ["%s=%s" % (name, _safe_value(name, fields[name]))
        for name in sorted(fields)]
    return event + (" " + " ".join(parts) if parts else "")


def log_event(event, level=logging.INFO, **fields):
    """Emits one structured log line on the "yamsi" logger. Secret and
    phone values in fields are masked or redacted before emission."""
    logger.log(level, format_event(event, fields))
