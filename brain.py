"""YAMSI Brain: the controlled natural-language layer.

Architecture (non-negotiable, enforced by code structure, not just by
convention):

    question/report -> AI/NLU interpretation -> validated typed intent
    -> authorization -> deterministic application query/action
    -> actual database result

NEVER: LLM -> arbitrary SQL. NEVER: LLM -> unrestricted database write.

Dependency note: this project's requirements.txt (fastapi, uvicorn, httpx
only) has no AI/LLM SDK installed. Stage 007C's "AI/NLU interpretation"
step is therefore DETERMINISTIC -- keyword classification (owner_query.
classify) plus simple entity matching against reference data the caller
supplies (interpret_owner_question below). No AI env var is required or
read by any code in this stage. If a real LLM is integrated later, it
would only ever replace the interpret_owner_question() step -- it would
produce the same candidate `intent` dict shape this function already
produces, which owner_query.validate_intent()/answer_from_intent() then
independently validates and authorizes exactly as today. The model would
never gain direct database or SQL access at any point. See the Stage 007C
report for the env var NAME (not value) such an integration would need.

Staff-side report interpretation (item 3) is implemented in
rule_engine.parse_poultry_report() -- the actual parser message_processor
already calls automatically for nughe_farms/warri. It is re-exported here
(parse_staff_report) so callers only need to import brain for both
directions of the Brain.
"""
from datetime import datetime, timedelta, timezone
from supabase_backend import rest_get
import owner_query
import rule_engine

# Re-export: staff report interpretation lives in rule_engine (the module
# message_processor actually calls); brain.py doesn't duplicate it.
parse_staff_report = rule_engine.parse_poultry_report

_DATE_HINTS = (("today", 0), ("yesterday", -1))


async def load_reference_data(tenant_id):
    """Live DB read of the employee/business/branch reference data
    interpret_owner_question() needs for entity matching -- never a cached
    or hard-coded list, so name-matching always reflects real rows."""
    employees = await rest_get("/rest/v1/biz_employees", {"tenant_id": "eq." + tenant_id, "select": "id,display_name"})
    businesses = await rest_get("/rest/v1/biz_businesses", {"tenant_id": "eq." + tenant_id, "select": "id,name"})
    branches = await rest_get("/rest/v1/biz_branches", {"tenant_id": "eq." + tenant_id, "select": "business_id,id,name"})
    business_names = {b["id"]: b["name"] for b in businesses}
    known_businesses = [{"business_id": branch["business_id"],
        "business_name": business_names.get(branch["business_id"], branch["business_id"]),
        "branch_id": branch["id"], "branch_name": branch["name"]} for branch in branches]
    known_employees = [{"id": e["id"], "display_name": e["display_name"]} for e in employees]
    return known_employees, known_businesses


def _extract_date(question_text, now=None):
    now = now or datetime.now(timezone.utc)
    lowered = question_text.lower()
    for hint, offset_days in _DATE_HINTS:
        if hint in lowered:
            return (now + timedelta(days=offset_days)).date().isoformat()
    return None


def _extract_employee(question_text, known_employees):
    """known_employees: iterable of {"id": ..., "display_name": ...}.
    Returns (employee_id_or_None, ambiguous_bool). Matches a known
    employee's first name or full name appearing in the question. NEVER
    guesses when more than one name matches."""
    lowered = question_text.lower()
    matches = [e for e in known_employees
        if e["display_name"].split()[0].lower() in lowered or e["display_name"].lower() in lowered]
    distinct_ids = {m["id"] for m in matches}
    if len(distinct_ids) == 1:
        return matches[0]["id"], False
    if len(distinct_ids) > 1:
        return None, True
    return None, False


def _extract_business_branch(question_text, known_businesses):
    """known_businesses: iterable of {"business_id","business_name",
    "branch_id","branch_name"}. Returns (business_id, branch_id,
    ambiguous_bool). NEVER guesses between two different businesses; if a
    business is clear but its branch isn't, still returns ambiguous rather
    than picking one."""
    lowered = question_text.lower()
    matches = [b for b in known_businesses
        if b["business_name"].lower() in lowered or b["business_id"].replace("_", " ").lower() in lowered]
    distinct_businesses = {m["business_id"] for m in matches}
    if len(distinct_businesses) > 1:
        return None, None, True
    if len(distinct_businesses) == 0:
        return None, None, False
    business_matches = matches
    branch_named = [m for m in business_matches if m["branch_name"].lower() in lowered]
    if len(branch_named) == 1:
        return branch_named[0]["business_id"], branch_named[0]["branch_id"], False
    distinct_branches = {m["branch_id"] for m in business_matches}
    if len(distinct_branches) == 1:
        return business_matches[0]["business_id"], business_matches[0]["branch_id"], False
    return business_matches[0]["business_id"], None, True


def interpret_owner_question(question_text, *, known_employees=(), known_businesses=(), now=None):
    """The AI/NLU interpretation step. Returns a candidate intent dict --
    NEVER executed by this function itself. If entity resolution is
    ambiguous, returns a clarification marker instead of guessing."""
    query_type = owner_query.classify(question_text)
    if query_type is None:
        return {"clarification_needed": False, "intent": {"query_type": None}}
    employee_id, employee_ambiguous = _extract_employee(question_text, known_employees)
    business_id, branch_id, scope_ambiguous = _extract_business_branch(question_text, known_businesses)
    if employee_ambiguous or scope_ambiguous:
        reason = ("More than one matching employee name was found; please name them more specifically."
            if employee_ambiguous else
            "More than one matching business/branch was found; please be more specific.")
        return {"clarification_needed": True, "intent": None, "reason": reason}
    date = _extract_date(question_text, now=now)
    return {"clarification_needed": False, "intent": {"query_type": query_type,
        "employee_id": employee_id, "business_id": business_id, "branch_id": branch_id, "date": date}}


async def answer_owner_question(tenant_id, question_text, *, is_owner, caller_assignments=(),
        known_employees=(), known_businesses=(), now=None):
    """Full pipeline: interpret (AI/NLU) -> validate -> authorize ->
    deterministic owner_query -> real database result. Ambiguous input asks
    for clarification instead of fabricating an answer; unrecognized
    intents and missing data both say so explicitly -- never invented."""
    interpretation = interpret_owner_question(question_text, known_employees=known_employees,
        known_businesses=known_businesses, now=now)
    if interpretation["clarification_needed"]:
        return {"query_type": None, "recorded": False, "clarification_needed": True,
            "answer": interpretation["reason"], "data": None}
    result = await owner_query.answer_from_intent(tenant_id, interpretation["intent"],
        is_owner=is_owner, caller_assignments=caller_assignments)
    result.setdefault("clarification_needed", False)
    return result
