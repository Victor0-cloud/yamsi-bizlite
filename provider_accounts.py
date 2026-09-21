"""Authoritative provider-account routing helpers.

A valid Meta signature proves the request came from the app; it does not
authorize every phone_number_id. This module resolves provider accounts
against the owner-registered biz_provider_accounts table (exact
tenant/business/branch/account mapping plus enabled state). Unknown,
disabled, or cross-scope accounts never authorize -- callers fail closed.

No hard-coded phone-number IDs live anywhere here: every value comes
from the database row or the caller's already-scoped arguments. This
module performs reads only, never writes.
"""

from supabase_backend import rest_get

SUPPORTED_PROVIDERS = frozenset({"whatsapp", "telegram"})


def normalize_account(value):
    """Trims a provider account for comparison. Returns None for blank or
    non-string input -- never a default account."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


async def find_account(tenant_id, provider, provider_account):
    """Returns the single enabled registry row for an exact
    (tenant, provider, account) match, or None when the account is
    unknown, disabled, or the arguments are blank. Never raises for a
    missing row (callers fail closed on None); database failures still
    raise DatabaseUnavailable."""
    account = normalize_account(provider_account)
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return None
    if provider not in SUPPORTED_PROVIDERS or account is None:
        return None
    rows = await rest_get("/rest/v1/biz_provider_accounts", {
        "tenant_id": "eq." + tenant_id.strip(),
        "provider": "eq." + provider,
        "provider_account": "eq." + account,
        "enabled": "eq.true",
        "select": "tenant_id,business_id,branch_id,provider,"
            "provider_account,enabled,label"})
    if len(rows) != 1:
        return None
    row = rows[0]
    # Defense in depth: the query already filters tenant and enabled, but
    # a row from another tenant reaching here (stale cache, permissive
    # caller) must never authorize -- fail closed.
    if row.get("tenant_id") != tenant_id.strip():
        return None
    if row.get("enabled") is not True:
        return None
    return row


async def is_account_authorized(tenant_id, business_id, branch_id,
        provider, provider_account):
    """True only when the account is registered AND enabled for exactly
    this tenant/business/branch. No fallback to another scope, ever."""
    row = await find_account(tenant_id, provider, provider_account)
    if row is None:
        return False
    return row.get("business_id") == business_id \
        and row.get("branch_id") == branch_id


async def list_accounts(tenant_id):
    """Owner visibility over registered accounts for one tenant. Returns
    scope, provider, account, enabled state, and label only -- no
    secrets exist on these rows."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return []
    return await rest_get("/rest/v1/biz_provider_accounts", {
        "tenant_id": "eq." + tenant_id.strip(),
        "order": "business_id.asc,branch_id.asc,provider_account.asc",
        "select": "business_id,branch_id,provider,provider_account,"
            "enabled,label"})
