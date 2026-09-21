"""Fail-closed WhatsApp configuration validation.

Reports whether each requirement is configured and structurally usable,
without ever returning a secret value: every check yields a boolean plus
a missing-item name. Dimensions:

- inbound_ready:  webhook verification token, app secret, and public
  webhook base URL are present and structurally usable;
- outbound_ready: access token and a supported Graph API version;
- database_ready: Supabase URL plus service-role credentials load;
- live_ready:    all of the above plus the owner API key that protects
  recovery routes.

Missing configuration fails closed and never silently selects a
test/default credential. Only the Graph API version has a default, and
it is the single explicitly supported version constant -- anything else
is rejected rather than coerced.
"""

import os

import outbound_whatsapp
from supabase_backend import credentials, DatabaseUnavailable

SUPPORTED_GRAPH_API_VERSIONS = frozenset({"v21.0"})

MIN_TOKEN_LENGTH = 16
MIN_SECRET_LENGTH = 16


def _present(name, minimum=1):
    value = os.environ.get(name, "")
    return isinstance(value, str) and len(value.strip()) >= minimum


def _public_base_url():
    for name in ("WHATSAPP_WEBHOOK_BASE_URL", "WEBHOOK_BASE_URL",
            "PUBLIC_BASE_URL"):
        value = os.environ.get(name, "")
        if isinstance(value, str) and value.strip().lower().startswith(
                "https://"):
            return True
    return False


def _graph_api_version_supported():
    configured = os.environ.get("WHATSAPP_GRAPH_API_VERSION",
        outbound_whatsapp.GRAPH_API_VERSION)
    return isinstance(configured, str) \
        and configured.strip() in SUPPORTED_GRAPH_API_VERSIONS


def _database_usable():
    try:
        credentials()
    except DatabaseUnavailable:
        return False
    return True


def check_whatsapp_readiness():
    """Pure-environment read plus one credential-shape check. Returns
    {"inbound_ready", "outbound_ready", "database_ready", "live_ready",
    "checks": {name: bool}, "missing": [check-name, ...]}. Secret values
    never appear in the result."""
    checks = {
        "verify_token": _present("WHATSAPP_VERIFY_TOKEN", MIN_TOKEN_LENGTH),
        "app_secret": _present("WHATSAPP_APP_SECRET", MIN_SECRET_LENGTH),
        "webhook_base_url": _public_base_url(),
        "access_token": _present("WHATSAPP_ACCESS_TOKEN", MIN_TOKEN_LENGTH),
        "graph_api_version": _graph_api_version_supported(),
        "supabase": _database_usable(),
        "owner_api_key": _present("YAMSI_API_KEY", MIN_TOKEN_LENGTH),
    }
    inbound_ready = checks["verify_token"] and checks["app_secret"] \
        and checks["webhook_base_url"]
    outbound_ready = checks["access_token"] and checks["graph_api_version"]
    database_ready = checks["supabase"]
    live_ready = inbound_ready and outbound_ready and database_ready \
        and checks["owner_api_key"]
    return {"inbound_ready": inbound_ready,
        "outbound_ready": outbound_ready,
        "database_ready": database_ready,
        "live_ready": live_ready,
        "checks": checks,
        "missing": sorted(name for name, ok in checks.items() if not ok)}
