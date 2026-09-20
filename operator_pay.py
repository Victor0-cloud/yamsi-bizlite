"""Stage 1: deterministic operator-pay preview.

An operator is NEVER paid automatically. This module computes a preview
only, from two inputs:

- confirmed good production bags (a count the caller supplies from
  confirmed operational records -- never from raw message text), and
- an owner-confirmed piece-rate setting (biz_setting_versions,
  key OPERATOR_PIECE_RATE_KEY, latest value {"amount_kobo": int}).

If the setting is absent or malformed, the preview reports
"rate not configured" instead of inventing a rate. The preview creates
no expense, no cash movement, and no other record: it performs zero
writes (one read for the setting, then pure arithmetic).
"""

from supabase_backend import rest_get

# Owner-confirmed piece-rate setting key in biz_setting_versions.
OPERATOR_PIECE_RATE_KEY = "operator_piece_rate_per_bag"


def _is_strict_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


async def fetch_piece_rate_kobo(tenant_id, business_id, branch_id):
    """Reads the owner-confirmed per-bag rate (integer kobo), or None when
    no usable setting exists. Never fabricates a default; a malformed
    value is treated identically to no setting at all."""
    rows = await rest_get("/rest/v1/biz_setting_versions", {
        "tenant_id": "eq." + tenant_id,
        "business_id": "eq." + business_id,
        "branch_id": "eq." + branch_id,
        "key": "eq." + OPERATOR_PIECE_RATE_KEY,
        "order": "effective_from.desc", "limit": "1",
        "select": "value"})
    if not rows:
        return None
    value = rows[0].get("value")
    if not isinstance(value, dict):
        return None
    rate = value.get("amount_kobo")
    if not _is_strict_int(rate) or rate <= 0:
        return None
    return rate


def preview_operator_pay(good_bags, rate_kobo):
    """Pure preview: {"status", "good_bags", "rate_kobo", "amount_kobo"}.

    Returns status "rate_not_configured" (with the exact message
    "rate not configured") when rate_kobo is None. Raises ValueError for
    invalid bag counts or malformed rates -- the caller supplies real
    values, never guesses.
    """
    if not _is_strict_int(good_bags) or good_bags < 0:
        raise ValueError("good_bags must be a non-negative integer")
    if rate_kobo is None:
        return {"status": "rate_not_configured",
            "message": "rate not configured",
            "good_bags": good_bags,
            "rate_kobo": None, "amount_kobo": None}
    if not _is_strict_int(rate_kobo) or rate_kobo <= 0:
        raise ValueError("rate_kobo must be a positive integer")
    return {"status": "ok",
        "good_bags": good_bags,
        "rate_kobo": rate_kobo,
        "amount_kobo": good_bags * rate_kobo}


async def preview_operator_pay_for_scope(tenant_id, business_id, branch_id,
        good_bags):
    """Loads the owner-confirmed rate for one scope, then previews. One
    read, zero writes; "rate not configured" when the setting is absent."""
    rate = await fetch_piece_rate_kobo(tenant_id, business_id, branch_id)
    return preview_operator_pay(good_bags, rate)
