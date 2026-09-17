"""Task engine: routine operational tasks (evidence requests, bird-care
follow-ups, escalations) plus the sensitive-action approval gate.

Sensitive actions (see SENSITIVE_ACTIONS) are never performed by this
codebase automatically -- they require an explicitly APPROVED
biz_approval_requests row, checked in application code, not left to an AI
prompt.
"""
from supabase_backend import rest_get, rest_post, rest_patch, DatabaseUnavailable

TASK_STATUSES = ("pending", "acknowledged", "in_progress", "completed", "overdue", "cancelled")

ALLOWED_TRANSITIONS = {
    "pending": {"acknowledged", "in_progress", "completed", "overdue", "cancelled"},
    "acknowledged": {"in_progress", "completed", "overdue", "cancelled"},
    "in_progress": {"completed", "overdue", "cancelled"},
    "overdue": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}

SENSITIVE_ACTIONS = (
    "fire_staff", "hire_staff", "salary_change", "disciplinary_action", "major_purchase",
    "transfer_money", "price_change", "compensation_change", "loan", "payment",
    "ownership_change", "other_sensitive",
)


class ApprovalRequired(Exception):
    """Raised when a sensitive action is attempted without an approved
    biz_approval_requests record."""


async def create_task(tenant_id, business_id, branch_id, task_type, title, *, created_by,
        source, assigned_employee_id=None, instructions=None, priority="normal",
        due_at=None, requires_evidence=False, related_inbox_id=None, related_submission_id=None,
        dedupe_key=None):
    """Idempotent: at most one task per (related_submission_id, task_type)
    when related_submission_id is given, or per dedupe_key otherwise --
    dedupe_key exists for tasks with no submission to key off, e.g. a
    "missing daily report" follow-up (by definition, no submission exists
    yet for a report that never arrived)."""
    if dedupe_key is not None:
        existing = await get_task_by_dedupe_key(dedupe_key)
        if existing is not None:
            return existing
    elif related_submission_id is not None:
        existing = await get_task_by_submission(related_submission_id, task_type)
        if existing is not None:
            return existing
    row = {
        "tenant_id": tenant_id, "business_id": business_id, "branch_id": branch_id,
        "assigned_employee_id": assigned_employee_id, "created_by": created_by, "source": source,
        "related_inbox_id": related_inbox_id, "related_submission_id": related_submission_id,
        "task_type": task_type, "title": title, "instructions": instructions,
        "priority": priority, "due_at": due_at, "requires_evidence": requires_evidence,
        "dedupe_key": dedupe_key,
    }
    await rest_post("/rest/v1/biz_tasks", [row],
        params={"on_conflict": "related_submission_id,task_type"} if related_submission_id else None)
    if dedupe_key is not None:
        return await get_task_by_dedupe_key(dedupe_key)
    if related_submission_id is not None:
        return await get_task_by_submission(related_submission_id, task_type)
    rows = await rest_get("/rest/v1/biz_tasks", {
        "tenant_id": "eq." + tenant_id, "task_type": "eq." + task_type,
        "order": "created_at.desc", "limit": "1"})
    return rows[0] if rows else None


async def get_task_by_dedupe_key(dedupe_key):
    rows = await rest_get("/rest/v1/biz_tasks", {"dedupe_key": "eq." + dedupe_key})
    return rows[0] if rows else None


async def get_task_by_submission(submission_id, task_type):
    rows = await rest_get("/rest/v1/biz_tasks",
        {"related_submission_id": "eq." + submission_id, "task_type": "eq." + task_type})
    return rows[0] if rows else None


async def transition_task(task_id, new_status, *, completed_by=None):
    if new_status not in TASK_STATUSES:
        raise ValueError("Unknown task status: " + new_status)
    current = await rest_get("/rest/v1/biz_tasks", {"id": "eq." + task_id, "select": "status"})
    if not current:
        raise DatabaseUnavailable("Task not found: " + task_id)
    current_status = current[0]["status"]
    if new_status != current_status and new_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
        raise ValueError("Illegal task transition: %s -> %s" % (current_status, new_status))
    body = {"status": new_status}
    if new_status == "completed":
        body["completed_by"] = completed_by
        from datetime import datetime, timezone
        body["completed_at"] = datetime.now(timezone.utc).isoformat()
    await rest_patch("/rest/v1/biz_tasks", {"id": "eq." + task_id}, body)


def assert_employee_authorized(employee_business_branches, business_id, branch_id):
    """employee_business_branches: iterable of (business_id, branch_id) pairs the
    employee is actually assigned to (from biz_assignments). Raises PermissionError
    if the requested scope isn't among them -- staff never act outside their own
    assigned business/branch."""
    if (business_id, branch_id) not in set(employee_business_branches):
        raise PermissionError("Employee is not assigned to %s/%s" % (business_id, branch_id))


async def request_approval(tenant_id, action_type, requested_by, subject_description, payload=None):
    if action_type not in SENSITIVE_ACTIONS:
        raise ValueError("Not a recognized sensitive action type: " + action_type)
    row = {"tenant_id": tenant_id, "action_type": action_type, "requested_by": requested_by,
        "subject_description": subject_description, "payload": payload or {}}
    await rest_post("/rest/v1/biz_approval_requests", [row], prefer="return=minimal")
    rows = await rest_get("/rest/v1/biz_approval_requests", {
        "tenant_id": "eq." + tenant_id, "action_type": "eq." + action_type,
        "requested_by": "eq." + requested_by, "status": "eq.pending",
        "order": "created_at.desc", "limit": "1"})
    return rows[0] if rows else None


async def decide_approval(request_id, decided_by, approve, notes=None):
    from datetime import datetime, timezone
    body = {"status": "approved" if approve else "rejected", "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc).isoformat(), "decision_notes": notes}
    await rest_patch("/rest/v1/biz_approval_requests", {"id": "eq." + request_id}, body)


async def enforce_approval(request_id):
    """Guard to call immediately before performing a sensitive action. Raises
    ApprovalRequired unless the referenced request is status='approved'."""
    rows = await rest_get("/rest/v1/biz_approval_requests", {"id": "eq." + request_id, "select": "status"})
    if not rows or rows[0]["status"] != "approved":
        raise ApprovalRequired("Action blocked: approval request %s is not approved" % request_id)
