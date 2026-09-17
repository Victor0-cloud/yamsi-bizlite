"""Owner intelligence: answers questions ONLY from real recorded rows.

No free-form SQL generation. A fixed, enumerated set of query functions is
exposed. Two entry points share one authorization + dispatch boundary:

- answer(): Stage 007's deterministic keyword classify() -- no AI/LLM call
  exists in this codebase yet.
- answer_from_intent(): the clean interface a FUTURE language-model layer
  targets. It must produce a validated {"query_type": ..., params...} intent
  dict -- never SQL, never a raw query string. validate_intent() is the
  application-code boundary that decides whether that intent is even
  well-formed before anything touches the database; dispatch() is the only
  thing allowed to turn a validated intent into a real read. There is no
  path anywhere in this codebase for a model to execute its own SQL.

If nothing matches, a required field is missing, or a query returns no
rows, the answer is explicit "not recorded" / "I don't understand" --
never fabricated.

Authorization: the owner sees everything. A non-owner caller is restricted
to the business/branch pairs in their own biz_assignments -- see
task_engine.assert_employee_authorized, reused here.
"""
from datetime import datetime, timedelta, timezone
from supabase_backend import rest_get
import task_engine

NOT_RECORDED = "Not recorded."

# daily_report_status/production_summary/mortality_summary are the
# Stage 007C-requested names; they route to the same underlying functions
# as the original submission_status/crate_count/mortality_this_week (kept
# for backward compatibility) -- one implementation each, not duplicated.
QUERY_TYPES = ("submission_status", "daily_report_status", "crate_count", "production_summary",
    "mortality_this_week", "mortality_summary", "missing_evidence", "outstanding_tasks",
    "employee_report_status", "business_summary")

# Keyword-presence based (order-independent, unlike a single ordered regex)
# so "Did Amos send today's report?" matches exactly like "What did Amos
# report today?". Still fully deterministic -- no AI/LLM call.
_MISSING_EVIDENCE_HINTS = ("proof", "evidence", "picture", "pictures", "photo", "photos")


def _has_any(lowered, *keywords):
    return any(keyword in lowered for keyword in keywords)


def classify(question_text):
    lowered = (question_text or "").lower()
    if "missing" in lowered and _has_any(lowered, *_MISSING_EVIDENCE_HINTS):
        return "missing_evidence"
    if _has_any(lowered, "crate", "crates"):
        return "crate_count"
    if _has_any(lowered, "mortality", "mortalities", "died", "dead"):
        return "mortality_this_week"
    if "outstanding" in lowered and "task" in lowered:
        return "outstanding_tasks"
    if _has_any(lowered, "what tasks", "which tasks"):
        return "outstanding_tasks"
    if _has_any(lowered, "report", "reports", "reported", "submit", "submitted", "submission"):
        return "submission_status"
    return None


# The validated-intent boundary a future NLU/LLM layer targets. Example:
# "What did Amos report today?" -> AI/NLU -> validated intent:
#   {"query_type": "submission_status", "business_id": "nughe_farms",
#    "branch_id": "warri", "employee_id": "<amos-uuid>", "date": "2026-09-16"}
# -> answer_from_intent() -> this module's own deterministic query -> real result.
INTENT_REQUIRED_PARAMS = {
    "submission_status": ("business_id", "branch_id", "employee_id"),
    "daily_report_status": ("business_id", "branch_id", "employee_id"),
    "crate_count": ("business_id", "branch_id"),
    "production_summary": ("business_id", "branch_id"),
    "mortality_this_week": ("business_id", "branch_id"),
    "mortality_summary": ("business_id", "branch_id"),
    "missing_evidence": (),
    "outstanding_tasks": (),
    "employee_report_status": ("employee_id",),
    "business_summary": ("business_id", "branch_id"),
}


def validate_intent(intent):
    """Returns (True, None) if intent is well-formed, else (False, reason).
    This is the only gate between "what a future language model produced"
    and "what actually runs" -- the model may interpret language, this
    function decides what's a permitted query."""
    query_type = intent.get("query_type")
    if query_type not in QUERY_TYPES:
        return False, "Unrecognized query_type"
    for field in INTENT_REQUIRED_PARAMS[query_type]:
        if not intent.get(field):
            return False, "Missing required field: " + field
    return True, None


async def submission_status(tenant_id, business_id, branch_id, employee_id, on_date):
    rows = await rest_get("/rest/v1/biz_submissions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "employee_id": "eq." + employee_id, "created_at": "gte." + on_date, "select": "id,kind,created_at"})
    if not rows:
        return {"recorded": False, "answer": NOT_RECORDED, "data": None}
    return {"recorded": True, "answer": "Yes, %d submission(s) recorded." % len(rows), "data": rows}


async def crate_count(tenant_id, business_id, branch_id, on_date):
    rows = await rest_get("/rest/v1/biz_submissions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "kind": "eq.poultry_daily_report", "created_at": "gte." + on_date, "select": "payload"})
    total = None
    for row in rows:
        value = ((row.get("payload") or {}).get("parsed") or {}).get("fields", {}).get("crates")
        if value is not None:
            total = (total or 0) + value
    if total is None:
        return {"recorded": False, "answer": NOT_RECORDED, "data": None}
    return {"recorded": True, "answer": "%s crates recorded." % total, "data": {"crates": total}}


async def mortality_this_week(tenant_id, business_id, branch_id, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat()
    rows = await rest_get("/rest/v1/biz_mortality_incidents", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "created_at": "gte." + since, "select": "mortality_count,created_at,suspected_cause"})
    if not rows:
        return {"recorded": False, "answer": NOT_RECORDED, "data": None}
    total = sum(r["mortality_count"] for r in rows)
    return {"recorded": True, "answer": "%d mortality incident(s) this week, %d bird(s) total." % (len(rows), total), "data": rows}


async def missing_evidence(tenant_id, business_id=None, branch_id=None):
    params = {"tenant_id": "eq." + tenant_id, "status": "in.(required,missing)", "select": "id,status,submission_id,created_at"}
    if business_id:
        params["submission_business_id"] = "eq." + business_id
    if branch_id:
        params["submission_branch_id"] = "eq." + branch_id
    rows = await rest_get("/rest/v1/biz_evidence", params)
    if not rows:
        return {"recorded": False, "answer": NOT_RECORDED, "data": None}
    return {"recorded": True, "answer": "%d report(s) missing required proof." % len(rows), "data": rows}


async def outstanding_tasks(tenant_id, business_id=None, branch_id=None):
    params = {"tenant_id": "eq." + tenant_id, "status": "in.(pending,acknowledged,in_progress,overdue)",
        "select": "id,task_type,status,assigned_employee_id,due_at"}
    if business_id:
        params["business_id"] = "eq." + business_id
    if branch_id:
        params["branch_id"] = "eq." + branch_id
    rows = await rest_get("/rest/v1/biz_tasks", params)
    if not rows:
        return {"recorded": False, "answer": NOT_RECORDED, "data": None}
    return {"recorded": True, "answer": "%d outstanding task(s)." % len(rows), "data": rows}


async def employee_report_status(tenant_id, employee_id, business_id=None, branch_id=None, now=None):
    """Trailing-7-day submission activity for one employee, optionally
    narrowed to a business/branch."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat()
    params = {"tenant_id": "eq." + tenant_id, "employee_id": "eq." + employee_id,
        "created_at": "gte." + since, "select": "id,kind,business_id,branch_id,created_at"}
    if business_id:
        params["business_id"] = "eq." + business_id
    if branch_id:
        params["branch_id"] = "eq." + branch_id
    rows = await rest_get("/rest/v1/biz_submissions", params)
    if not rows:
        return {"recorded": False, "answer": NOT_RECORDED, "data": None}
    return {"recorded": True, "answer": "%d submission(s) in the last 7 days." % len(rows), "data": rows}


async def business_summary(tenant_id, business_id, branch_id, on_date=None, now=None):
    """Aggregate view: today's crate count, this week's mortality, missing
    evidence, and outstanding tasks for one business/branch -- each still
    computed by the same single-purpose function, never a separate query."""
    on_date = on_date or datetime.now(timezone.utc).date().isoformat()
    crates = await crate_count(tenant_id, business_id, branch_id, on_date)
    mortality = await mortality_this_week(tenant_id, business_id, branch_id, now)
    missing = await missing_evidence(tenant_id, business_id, branch_id)
    tasks = await outstanding_tasks(tenant_id, business_id, branch_id)
    parts = (crates, mortality, missing, tasks)
    recorded = any(part["recorded"] for part in parts)
    return {"recorded": recorded, "answer": " ".join(part["answer"] for part in parts),
        "data": {"crates": crates, "mortality": mortality, "missing_evidence": missing, "outstanding_tasks": tasks}}


async def dispatch(tenant_id, query_type, *, business_id=None, branch_id=None, employee_id=None,
        on_date=None, now=None):
    """The only function allowed to turn a query_type + params into a real
    database read. Assumes the caller (answer_from_intent) already validated
    and authorized the request."""
    on_date = on_date or datetime.now(timezone.utc).date().isoformat()
    if query_type in ("submission_status", "daily_report_status"):
        return await submission_status(tenant_id, business_id, branch_id, employee_id, on_date)
    if query_type in ("crate_count", "production_summary"):
        return await crate_count(tenant_id, business_id, branch_id, on_date)
    if query_type in ("mortality_this_week", "mortality_summary"):
        return await mortality_this_week(tenant_id, business_id, branch_id, now)
    if query_type == "missing_evidence":
        return await missing_evidence(tenant_id, business_id, branch_id)
    if query_type == "employee_report_status":
        return await employee_report_status(tenant_id, employee_id, business_id, branch_id, now)
    if query_type == "business_summary":
        return await business_summary(tenant_id, business_id, branch_id, on_date, now)
    return await outstanding_tasks(tenant_id, business_id, branch_id)


async def answer_from_intent(tenant_id, intent, *, is_owner, caller_assignments=()):
    """The clean interface a future NLU/LLM layer targets -- see module
    docstring. `intent` must already be the model's best-effort structured
    guess; this function is what actually decides whether it's allowed to
    run, never the model itself."""
    valid, _reason = validate_intent(intent)
    if not valid:
        return {"query_type": intent.get("query_type"), "recorded": False,
            "answer": "I don't understand that question yet.", "data": None}
    query_type = intent["query_type"]
    business_id, branch_id = intent.get("business_id"), intent.get("branch_id")
    if not is_owner and business_id and branch_id:
        try:
            task_engine.assert_employee_authorized(caller_assignments, business_id, branch_id)
        except PermissionError:
            return {"query_type": query_type, "recorded": False,
                "answer": "You are not authorized to see that business/branch.", "data": None}
    result = await dispatch(tenant_id, query_type, business_id=business_id, branch_id=branch_id,
        employee_id=intent.get("employee_id"), on_date=intent.get("date"), now=intent.get("now"))
    result["query_type"] = query_type
    return result


async def answer(tenant_id, question_text, *, is_owner, caller_assignments=(), business_id=None,
        branch_id=None, employee_id=None, on_date=None, now=None):
    """Stage 007's deterministic keyword-based entry point. caller_assignments:
    iterable of (business_id, branch_id) the caller is actually assigned to
    (ignored when is_owner=True). Builds the same validated intent shape
    answer_from_intent() expects, so both entry points share one
    authorization + dispatch boundary."""
    intent = {"query_type": classify(question_text), "business_id": business_id, "branch_id": branch_id,
        "employee_id": employee_id, "date": on_date, "now": now}
    return await answer_from_intent(tenant_id, intent, is_owner=is_owner, caller_assignments=caller_assignments)
