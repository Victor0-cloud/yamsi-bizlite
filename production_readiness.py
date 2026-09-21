"""Single production-readiness validator for YAMSI BizLite.

validate_environment() inspects environment-variable *names and shapes*
only and classifies every known variable as one of:

- "ok"               present and structurally usable;
- "missing"          required but absent/blank (fail closed in production);
- "malformed"        present but structurally unusable (bad URL, bad
                     version format, unparseable number/boolean, known
                     placeholder, too short);
- "conflicting"      two sources disagree (canonical webhook base URL and
                     a legacy fallback set to different values);
- "optional_unset"   optional and absent (a compiled-in default applies);
- "defaulted"        optional and absent while production mode itself
                     defaults to fail-closed production behavior.

build_readiness_report() turns that classification into the deterministic,
fully redacted payload served by GET /internal/whatsapp/readiness: overall
ready flag, per-dimension status (database, provider, webhook,
outbound_dispatch), enabled provider-account count, warnings, and safe
remediation naming variable names only.

Secret values never appear anywhere in either result -- not even their
lengths. No network call is made here: database usability is the same
offline credential-shape check the rest of the codebase uses, and the
optional account count is a single guarded registry read performed by the
endpoint, degrading to null (never an exception) when unreachable.
"""

import os
import re
from urllib.parse import urlparse

import outbound_whatsapp
from supabase_backend import credentials, DatabaseUnavailable

MIN_SECRET_LENGTH = 16

# Required in production; a missing/malformed entry fails the deployment
# closed (ready=false) instead of selecting a default credential.
REQUIRED_VARS = (
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
    "SUPABASE_URL",
    "SUPABASE_SECRET_KEY",
    "YAMSI_API_KEY",
)

# Webhook base URL is required for inbound work; the canonical name wins.
# The two legacy names keep older local setups working but are reported.
BASE_URL_VAR = "WHATSAPP_WEBHOOK_BASE_URL"
LEGACY_BASE_URL_VARS = ("WEBHOOK_BASE_URL", "PUBLIC_BASE_URL")

# Optional knobs: absent means a compiled-in default applies.
OPTIONAL_VARS = (
    "WHATSAPP_GRAPH_API_VERSION",
    "WHATSAPP_ENV",
    "OUTBOUND_HTTP_TIMEOUT_SECONDS",
)

SUPPORTED_GRAPH_API_VERSIONS = frozenset({"v21.0"})
DEFAULT_GRAPH_API_VERSION = "v21.0"

ENV_MODES = ("production", "local", "test")

MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 10.0

# Values that are obviously not real configuration. Test sentinels
# ("test-only-...") are deliberately NOT in this set: they are long,
# explicit throwaways, and local tests must stay usable.
_PLACEHOLDERS = frozenset({
    "changeme", "change_me", "change-me", "password", "secret",
    "placeholder", "example", "todo", "xxx", "your_secret_key_here",
    "your-secret-key-here", "replace_me", "replace-me",
})


def _raw(name):
    value = os.environ.get(name, "")
    return value if isinstance(value, str) else ""


def _is_placeholder(value):
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.lower() in _PLACEHOLDERS:
        return True
    return stripped.startswith("<") and stripped.endswith(">")


def _check_token(name):
    value = _raw(name)
    if not value.strip():
        return "missing"
    if _is_placeholder(value) or len(value.strip()) < MIN_SECRET_LENGTH:
        return "malformed"
    return "ok"


def _check_supabase_url():
    value = _raw("SUPABASE_URL").strip().rstrip("/")
    if not value:
        return "missing"
    # Same shape the live credential loader enforces: no other host is
    # ever accepted, so a typo fails here instead of at first use.
    if not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co", value):
        return "malformed"
    return "ok"


def _check_supabase_key():
    value = _raw("SUPABASE_SECRET_KEY")
    if not value.strip():
        return "missing"
    if _is_placeholder(value):
        return "malformed"
    return "ok"


def _https_url_ok(value):
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _check_base_url(variables):
    canonical = _raw(BASE_URL_VAR)
    legacy = [(name, _raw(name)) for name in LEGACY_BASE_URL_VARS]
    legacy_set = [(name, value) for name, value in legacy if value.strip()]
    if canonical.strip():
        if not _https_url_ok(canonical):
            variables[BASE_URL_VAR] = "malformed"
            return None
        for name, value in legacy_set:
            if value.strip() != canonical.strip():
                variables[BASE_URL_VAR] = "conflicting"
                variables[name] = "conflicting"
                return None
            variables[name] = "ok"
        variables[BASE_URL_VAR] = "ok"
        return canonical.strip()
    for name, _value in legacy:
        variables[name] = "optional_unset"
    if legacy_set:
        name, value = legacy_set[0]
        variables[BASE_URL_VAR] = "missing"
        if not _https_url_ok(value):
            variables[name] = "malformed"
            return None
        variables[name] = "ok"
        return value.strip()
    variables[BASE_URL_VAR] = "missing"
    return None


def _check_api_version(variables):
    value = _raw("WHATSAPP_GRAPH_API_VERSION")
    if not value.strip():
        variables["WHATSAPP_GRAPH_API_VERSION"] = "defaulted"
        return DEFAULT_GRAPH_API_VERSION
    text = value.strip()
    if not re.fullmatch(r"v\d+\.\d+", text) \
            or text not in SUPPORTED_GRAPH_API_VERSIONS:
        variables["WHATSAPP_GRAPH_API_VERSION"] = "malformed"
        return None
    variables["WHATSAPP_GRAPH_API_VERSION"] = "ok"
    return text


def _check_env_mode(variables):
    value = _raw("WHATSAPP_ENV")
    if not value.strip():
        # Fail closed: an unset mode is treated as production.
        variables["WHATSAPP_ENV"] = "defaulted"
        return True
    if value.strip().lower() not in ENV_MODES:
        variables["WHATSAPP_ENV"] = "malformed"
        return True
    variables["WHATSAPP_ENV"] = "ok"
    return value.strip().lower() == "production"


def _check_timeout(variables):
    value = _raw("OUTBOUND_HTTP_TIMEOUT_SECONDS")
    if not value.strip():
        variables["OUTBOUND_HTTP_TIMEOUT_SECONDS"] = "defaulted"
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = float(value.strip())
    except ValueError:
        variables["OUTBOUND_HTTP_TIMEOUT_SECONDS"] = "malformed"
        return None
    if not MIN_TIMEOUT_SECONDS <= seconds <= MAX_TIMEOUT_SECONDS:
        variables["OUTBOUND_HTTP_TIMEOUT_SECONDS"] = "malformed"
        return None
    variables["OUTBOUND_HTTP_TIMEOUT_SECONDS"] = "ok"
    return seconds


def validate_environment():
    """Classifies every known variable without exposing any value.

    Returns {"production_mode", "ready", "variables", "missing",
    "malformed", "conflicting", "warnings", "remediation"}. "ready" is
    true only when nothing required is missing and nothing set is
    malformed or conflicting. Local/test setups stay usable: the result
    is a report, never an exception, and tests drive it with throwaway
    fixtures through patch.dict.
    """
    variables = {}
    warnings = []
    remediation = []
    for name in ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
            "WHATSAPP_ACCESS_TOKEN", "YAMSI_API_KEY"):
        variables[name] = _check_token(name)
    variables["SUPABASE_URL"] = _check_supabase_url()
    variables["SUPABASE_SECRET_KEY"] = _check_supabase_key()
    base_url = _check_base_url(variables)
    api_version = _check_api_version(variables)
    production_mode = _check_env_mode(variables)
    timeout = _check_timeout(variables)
    if variables[BASE_URL_VAR] == "missing" and base_url is not None:
        warnings.append(
            "Using deprecated %s; set %s instead." % (
                next(n for n in LEGACY_BASE_URL_VARS
                    if variables.get(n) == "ok"), BASE_URL_VAR))
    missing = sorted(name for name, status in variables.items()
        if status == "missing" and (
            name in REQUIRED_VARS or name == BASE_URL_VAR))
    malformed = sorted(name for name, status in variables.items()
        if status == "malformed")
    conflicting = sorted(name for name, status in variables.items()
        if status == "conflicting")
    for name in missing:
        remediation.append("Set %s in the private Render environment." % name)
    for name in malformed:
        if name == "SUPABASE_URL":
            remediation.append(
                "Set SUPABASE_URL to the exact https://<PROJECT_REF>.supabase.co URL.")
        elif name == BASE_URL_VAR:
            remediation.append(
                "Set %s to the public https:// callback origin." % BASE_URL_VAR)
        elif name == "WHATSAPP_GRAPH_API_VERSION":
            remediation.append(
                "Set WHATSAPP_GRAPH_API_VERSION to %s (only supported value)."
                % DEFAULT_GRAPH_API_VERSION)
        elif name == "WHATSAPP_ENV":
            remediation.append(
                "Set WHATSAPP_ENV to one of: production, local, test.")
        elif name == "OUTBOUND_HTTP_TIMEOUT_SECONDS":
            remediation.append(
                "Set OUTBOUND_HTTP_TIMEOUT_SECONDS to a number between %g and %g."
                % (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS))
        else:
            remediation.append(
                "Replace %s with a generated value (%d+ random characters)."
                % (name, MIN_SECRET_LENGTH))
    for name in conflicting:
        remediation.append(
            "Unset the legacy %s entries so only %s remains."
            % (name, BASE_URL_VAR))
    ready = not missing and not malformed and not conflicting
    if not production_mode:
        warnings.append(
            "Non-production mode: live sending stays disabled until "
            "WHATSAPP_ENV=production with full configuration.")
    return {"production_mode": production_mode, "ready": ready,
        "variables": variables, "missing": missing,
        "malformed": malformed, "conflicting": conflicting,
        "warnings": warnings, "remediation": remediation}


def _database_dimension():
    try:
        credentials()
    except DatabaseUnavailable:
        return {"ready": False, "detail": "supabase_url_or_key_not_configured"}
    return {"ready": True, "detail": "supabase_url_and_key_configured"}


def _provider_dimension(variables):
    if variables.get("WHATSAPP_ACCESS_TOKEN") == "ok" and \
            variables.get("WHATSAPP_GRAPH_API_VERSION") in ("ok", "defaulted"):
        return {"ready": True,
            "detail": "access_token_and_supported_api_version_configured"}
    return {"ready": False, "detail": "access_token_or_api_version_missing"}


def _webhook_dimension(variables, base_url):
    if variables.get("WHATSAPP_VERIFY_TOKEN") == "ok" and \
            variables.get("WHATSAPP_APP_SECRET") == "ok" \
            and base_url is not None:
        return {"ready": True,
            "detail": "verify_token_secret_and_callback_configured"}
    return {"ready": False, "detail": "verify_token_secret_or_callback_missing"}


def _dispatch_dimension(variables):
    import outbound_dispatch_worker
    bounded = outbound_dispatch_worker.OUTBOUND_MAX_ATTEMPTS >= 1 \
        and outbound_dispatch_worker.RETRY_BASE_DELAY_MINUTES >= 1
    if bounded and variables.get("WHATSAPP_ACCESS_TOKEN") == "ok" and \
            variables.get("OUTBOUND_HTTP_TIMEOUT_SECONDS") in ("ok", "defaulted"):
        return {"ready": True,
            "detail": "bounded_retry_lease_and_recovery_configured"}
    return {"ready": False, "detail": "outbound_sending_not_configured"}


async def build_readiness_report(tenant_id=None):
    """Deterministic redacted readiness payload for the internal endpoint.

    tenant_id is optional: when supplied and the registry is reachable,
    enabled_provider_account_count carries that tenant's enabled account
    count; otherwise it is null with a warning explaining why. Never
    raises for an unreachable database and never performs a Meta call,
    so a transient provider outage cannot break this endpoint.
    """
    env = validate_environment()
    variables = env["variables"]
    base_url = _raw(BASE_URL_VAR).strip() or None
    if base_url is not None and not _https_url_ok(base_url):
        base_url = None
        for name in LEGACY_BASE_URL_VARS:
            candidate = _raw(name).strip()
            if candidate and _https_url_ok(candidate):
                base_url = candidate
                break
    dimensions = {
        "database": _database_dimension(),
        "provider": _provider_dimension(variables),
        "webhook": _webhook_dimension(variables, base_url),
        "outbound_dispatch": _dispatch_dimension(variables),
    }
    warnings = list(env["warnings"])
    count = None
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        warnings.append(
            "tenant_id not supplied: enabled provider-account count unavailable.")
    else:
        try:
            import provider_accounts
            rows = await provider_accounts.list_accounts(tenant_id.strip())
            count = sum(1 for row in rows if row.get("enabled") is True)
        except (DatabaseUnavailable, Exception):
            warnings.append(
                "Provider registry unreachable: enabled provider-account count unavailable.")
    ready = env["ready"] and all(
        dimension["ready"] for dimension in dimensions.values())
    return {"ready": ready, "production_mode": env["production_mode"],
        "dimensions": dimensions,
        "enabled_provider_account_count": count,
        "missing": env["missing"], "malformed": env["malformed"],
        "conflicting": env["conflicting"], "warnings": warnings,
        "remediation": env["remediation"]}
