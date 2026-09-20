"""Stage 1: deterministic operator-pay preview.

An operator is NEVER paid automatically. This module computes a preview
only, from two authoritative inputs:

- confirmed good production bags, summed by preview_operator_pay_for_work
  from status='confirmed' biz_production_runs rows for the exact tenant,
  business, branch, operator, and work period -- never from raw message
  text, never from drafts, never from a caller-supplied total, and never
  including damaged/rejected bags, and
- an owner-confirmed piece-rate setting (biz_setting_versions,
  key OPERATOR_PIECE_RATE_KEY, latest value {"amount_kobo": int}).

If the setting is absent or malformed, the preview reports
"rate not configured" instead of inventing a rate. The preview creates
no expense, no payment, no cash movement, and no other record: it
performs reads only, zero writes.
"""

import uuid as uuid_module
from datetime import date

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


def _require_scope(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-blank string" % field)
    return value.strip()


def _require_operator_id(value):
    if not isinstance(value, str):
        raise ValueError("operator_id must be a UUID string")
    try:
        uuid_module.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("operator_id must be a valid UUID")
    return value


def _require_work_date(value, field):
    if not isinstance(value, str):
        raise ValueError("%s must be a YYYY-MM-DD string" % field)
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError("%s must be a YYYY-MM-DD date" % field) from None


async def _fetch_confirmed_good_bags(tenant_id, business_id, branch_id,
        operator_id, date_from, date_to):
    """Sums good_quantity over status='confirmed' production runs for the
    exact scope, operator, and inclusive work period. Draft/voided runs
    are excluded by the status filter; damaged/rejected bags live in
    rejected_quantity, which is never selected. Read-only."""
    rows = await rest_get("/rest/v1/biz_production_runs", {
        "tenant_id": "eq." + tenant_id,
        "business_id": "eq." + business_id,
        "branch_id": "eq." + branch_id,
        "produced_by": "eq." + operator_id,
        "status": "eq.confirmed",
        "and": "(production_date.gte.%s,production_date.lte.%s)"
            % (date_from.isoformat(), date_to.isoformat()),
        "select": "good_quantity"})
    total = 0
    for row in rows:
        quantity = row.get("good_quantity")
        if not _is_strict_int(quantity) or quantity < 0:
            raise ValueError(
                "Confirmed production holds an invalid good_quantity")
        total += quantity
    return total


async def _require_scoped_operator(tenant_id, business_id, branch_id,
        operator_id):
    """Validates the operator against authoritative scoped records: an
    active employee row in this tenant holding an assignment in this
    business/branch. Anything else raises instead of previewing."""
    employees = await rest_get("/rest/v1/biz_employees", {
        "tenant_id": "eq." + tenant_id, "id": "eq." + operator_id,
        "select": "id,active"})
    if len(employees) != 1 or employees[0].get("active") is not True:
        raise ValueError(
            "operator is not an active employee in this tenant")
    assignments = await rest_get("/rest/v1/biz_assignments", {
        "tenant_id": "eq." + tenant_id,
        "employee_id": "eq." + operator_id,
        "business_id": "eq." + business_id,
        "branch_id": "eq." + branch_id,
        "select": "business_id"})
    if not assignments:
        raise ValueError(
            "operator is not assigned to this business/branch")


async def preview_operator_pay_for_work(tenant_id, business_id, branch_id,
        operator_id, date_from, date_to):
    """Authoritative pay preview for one operator and one inclusive work
    period (YYYY-MM-DD strings). The bag count is calculated inside this
    function from confirmed production records -- there is deliberately no
    bag-total parameter, so no caller can inject a count. Read-only:
    three reads (operator, production, rate), zero writes.

    Returns {"status", "operator_id", "period", "good_bags", "rate_kobo",
    "amount_kobo"} with status "ok", or status "rate_not_configured"
    (message "rate not configured", amounts None) when no valid
    owner-confirmed rate exists. The confirmed bag total and source
    period are always reported either way.
    """
    tenant = _require_scope(tenant_id, "tenant_id")
    business = _require_scope(business_id, "business_id")
    branch = _require_scope(branch_id, "branch_id")
    operator = _require_operator_id(operator_id)
    start = _require_work_date(date_from, "date_from")
    end = _require_work_date(date_to, "date_to")
    if start > end:
        raise ValueError("date_from must not be after date_to")
    await _require_scoped_operator(tenant, business, branch, operator)
    good_bags = await _fetch_confirmed_good_bags(
        tenant, business, branch, operator, start, end)
    period = {"from": start.isoformat(), "to": end.isoformat()}
    rate = await fetch_piece_rate_kobo(tenant, business, branch)
    if rate is None:
        return {"status": "rate_not_configured",
            "message": "rate not configured",
            "operator_id": operator, "period": period,
            "good_bags": good_bags,
            "rate_kobo": None, "amount_kobo": None}
    preview = preview_operator_pay(good_bags, rate)
    preview["operator_id"] = operator
    preview["period"] = period
    return preview
