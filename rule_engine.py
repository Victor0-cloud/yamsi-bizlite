"""Business-rule engine. Deterministic only -- no AI/LLM call in Stage 007.

The evidence/photo-proof rules in this module apply ONLY to
(business_id, branch_id) == NUGHE_FARMS_WARRI. Every other business/branch
is left untouched; these are configurable-in-spirit rules kept in one place
rather than scattered `if business_id==...` checks across the codebase.

Reminder/escalation timing is NEVER fabricated: get_reminder_policy() reads
public.biz_setting_versions (key='evidence_reminder_policy', scoped to the
business/branch) for an owner-confirmed
{"max_attempts": int, "interval_minutes": int, "escalation_hours": int}
value. If no such row exists, reminders are still created (so the pending
follow-up is tracked) but with next_due_at/max_attempts left NULL and
policy_configured=False is reported back -- never a made-up cadence.
"""
import re
from datetime import datetime, timedelta, timezone
from supabase_backend import rest_get, rest_post, rest_patch
import task_engine
import evidence_store
import notifier

NUGHE_FARMS_WARRI = ("nughe_farms", "warri")
REMINDER_POLICY_KEY = "evidence_reminder_policy"
DAILY_REPORT_POLICY_KEY = "daily_reporting_requirement"

_EGGS = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*eggs?\b")
_CRATES = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*crates?\b")
_FEED = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(?:bags?|kg)\s*(?:of\s+)?feed\b")
_MORTALITY = re.compile(r"(?i)(?:(\d+)\s*(?:birds?|chicks?)?\s*(?:died|dead)\b|mortality[:\s]+(\d+))")
_SICK = re.compile(r"(?i)(\d+)\s*(?:sick|injured)\b")
_EXPENSE = re.compile(r"(?i)(?:spent|expense[s]?)[:\s]+(?:ngn|₦)?\s*(\d+(?:\.\d+)?)")
_CAUSE = re.compile(r"(?i)(?:cause|because of|due to)[:\s]+([a-zA-Z ,]+)")
_NO_MORTALITY = re.compile(r"(?i)\bno\s+(?:mortality|deaths?|bird\w*\s+died)\b")

# Small-number words staff commonly use instead of digits, e.g.
# "four and half bags feed today". Not exhaustive -- deliberately limited to
# what a single-branch farm report plausibly says.
_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_WORD_FEED = re.compile(
    r"(?i)\b(%s)\b(\s+and\s+(?:a\s+)?half)?\s*(?:bags?|kg)\s*(?:of\s+)?feed\b" % "|".join(_NUMBER_WORDS))


def _grab(pattern, text, key, fields, provenance, missing, caster=float):
    match = pattern.search(text)
    if not match:
        missing.append(key)
        return
    value = next(g for g in match.groups() if g is not None)
    fields[key] = caster(value)
    provenance[key] = "staff_reported"


def _grab_feed(text, fields, provenance, missing):
    """Feed is the one field staff plausibly write as a number-word
    ("four and half bags feed") rather than a digit -- tries the digit form
    first, then the word form, before giving up and listing it as missing."""
    match = _FEED.search(text)
    if match:
        fields["feed_used"] = float(match.group(1))
        provenance["feed_used"] = "staff_reported"
        return
    word_match = _WORD_FEED.search(text)
    if word_match:
        value = _NUMBER_WORDS[word_match.group(1).lower()]
        if word_match.group(2):
            value += 0.5
        fields["feed_used"] = float(value)
        provenance["feed_used"] = "staff_reported"
        return
    missing.append("feed_used")


def parse_poultry_report(text):
    """Deterministic. Only reports fields explicitly present in the text;
    a field never found is listed in missing_fields, never guessed."""
    fields, provenance, missing, errors = {}, {}, [], []
    if not text or not text.strip():
        return {"fields": fields, "provenance": provenance,
            "missing_fields": ["eggs_produced", "crates", "feed_used", "mortality_count"],
            "errors": ["Empty or missing message text"]}
    _grab(_EGGS, text, "eggs_produced", fields, provenance, missing)
    _grab(_CRATES, text, "crates", fields, provenance, missing)
    _grab_feed(text, fields, provenance, missing)
    if _NO_MORTALITY.search(text):
        fields["mortality_count"] = 0
        provenance["mortality_count"] = "staff_reported"
    else:
        _grab(_MORTALITY, text, "mortality_count", fields, provenance, missing, caster=int)
    _grab(_SICK, text, "sick_injured_count", fields, provenance, missing, caster=int)
    _grab(_EXPENSE, text, "expenses_ngn", fields, provenance, missing)
    cause_match = _CAUSE.search(text)
    if cause_match:
        fields["suspected_cause"] = cause_match.group(1).strip()
        provenance["suspected_cause"] = "staff_reported"
    # Free text is never discarded even when nothing structured was recognized:
    # the original message_text is always preserved alongside this by the caller.
    if not any(key in fields for key in ("eggs_produced", "crates", "mortality_count", "feed_used")):
        errors.append("No recognized poultry report fields in message")
    return {"fields": fields, "provenance": provenance, "missing_fields": missing, "errors": errors}


def extract(business_id, branch_id, text, received_at=None):
    """Business-scoped extraction dispatch. Only nughe_farms/warri gets the
    poultry parser; every other business/branch is untouched."""
    if (business_id, branch_id) == NUGHE_FARMS_WARRI:
        parsed = parse_poultry_report(text)
        if received_at:
            date_part = received_at[:10] if isinstance(received_at, str) else None
            if date_part:
                parsed["fields"]["reporting_date"] = date_part
                parsed["provenance"]["reporting_date"] = "system_derived"
            else:
                parsed["missing_fields"].append("reporting_date")
        else:
            parsed["missing_fields"].append("reporting_date")
        return {"kind": "poultry_daily_report", **parsed}
    from message_processor import parse_message
    sale = parse_message(text)
    return {"kind": sale["intent"] or "whatsapp_message", "intent": sale["intent"],
        "fields": sale["fields"], "missing_fields": sale["missing_fields"], "errors": sale["errors"]}


async def _resolve_employee_by_name(tenant_id, display_name):
    rows = await rest_get("/rest/v1/biz_employees",
        {"tenant_id": "eq." + tenant_id, "display_name": "eq." + display_name, "select": "id"})
    return rows[0]["id"] if rows else None


async def _resolve_phone_number_id(inbox_id):
    """The business's own WhatsApp number id, reused from the inbound
    webhook's metadata.phone_number_id (already stored as provider_account
    on the originating inbox row) -- never a new hard-coded value."""
    if inbox_id is None:
        return None
    rows = await rest_get("/rest/v1/biz_message_inbox", {"id": "eq." + inbox_id, "select": "provider_account"})
    return rows[0]["provider_account"] if rows else None


# The typed shape an owner-confirmed evidence_reminder_policy value must
# have. Not a schema constraint (biz_setting_versions.value is jsonb) --
# validate_reminder_policy() is the application-layer gate instead, so a
# malformed or half-filled config is treated the same as no config at all,
# never partially trusted.
REMINDER_POLICY_FIELDS = (
    "enabled",                      # bool -- required
    "first_reminder_delay_minutes", # int/float -- delay before the first nudge
    "repeat_interval_minutes",      # int/float -- gap between subsequent nudges
    "max_reminders",                # int -- attempts before giving up (-> biz_reminders.max_attempts)
    "escalation_hours",             # int/float -- how long 'pending' before escalate_stale_incidents fires
)


def validate_reminder_policy(value):
    """Returns (True, None) if value is a well-formed policy object, else
    (False, reason). A missing or malformed policy is never partially
    applied -- get_reminder_policy() treats it as fully unconfigured."""
    if not isinstance(value, dict):
        return False, "Policy must be an object"
    if not isinstance(value.get("enabled"), bool):
        return False, "Policy must include a boolean 'enabled' field"
    for field in REMINDER_POLICY_FIELDS[1:]:
        if field in value and value[field] is not None and not isinstance(value[field], (int, float)):
            return False, "%s must be numeric" % field
    return True, None


async def get_reminder_policy(tenant_id, business_id, branch_id):
    """Owner-confirmed, validated, and enabled policy from
    biz_setting_versions, or None if nothing usable has been configured
    yet. Never fabricates a default; a malformed or disabled policy is
    treated identically to no policy at all."""
    rows = await rest_get("/rest/v1/biz_setting_versions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "key": "eq." + REMINDER_POLICY_KEY, "order": "effective_from.desc", "limit": "1", "select": "value"})
    if not rows:
        return None
    value = rows[0]["value"]
    valid, _reason = validate_reminder_policy(value)
    if not valid or not value.get("enabled"):
        return None
    return value


async def _ensure_reminder(tenant_id, business_id, branch_id, evidence_id):
    """Idempotent: at most one reminder row per evidence row. Uses an
    owner-confirmed policy if one exists; otherwise the reminder is still
    created (so the pending follow-up is tracked) with no invented cadence."""
    existing = await rest_get("/rest/v1/biz_reminders", {"evidence_id": "eq." + evidence_id})
    policy = await get_reminder_policy(tenant_id, business_id, branch_id)
    if existing:
        return {"reminder_id": existing[0]["id"], "policy_configured": policy is not None}
    row = {"tenant_id": tenant_id, "subject_type": "evidence", "evidence_id": evidence_id}
    if policy:
        if policy.get("max_reminders") is not None:
            row["max_attempts"] = policy["max_reminders"]
        delay = policy.get("first_reminder_delay_minutes")
        if delay is not None:
            row["next_due_at"] = (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()
    await rest_post("/rest/v1/biz_reminders", [row], prefer="return=minimal")
    rows = await rest_get("/rest/v1/biz_reminders", {"evidence_id": "eq." + evidence_id})
    return {"reminder_id": rows[0]["id"] if rows else None, "policy_configured": policy is not None}


async def apply_rules(tenant_id, business_id, branch_id, employee_id, submission_id, extraction, inbox_id=None):
    """No-op outside nughe_farms/warri. For that scope: requires a crate
    photo when crates were reported, and creates a mortality incident +
    Phillip follow-up task when mortality_count > 0. Queues (never sends)
    the corresponding WhatsApp follow-ups."""
    if (business_id, branch_id) != NUGHE_FARMS_WARRI:
        return {"applied": False}
    fields = extraction.get("fields", {})
    result = {"applied": True, "evidence_id": None, "mortality_incident_id": None, "phillip_task_id": None,
        "reminder_policy_configured": None, "queued_messages": []}
    phone_number_id = await _resolve_phone_number_id(inbox_id)

    if "crates" in fields:
        evidence = await evidence_store.create_requirement(tenant_id, business_id, branch_id, submission_id,
            employee_id=employee_id)
        result["evidence_id"] = evidence["id"] if evidence else None
        task = await task_engine.create_task(tenant_id, business_id, branch_id, "evidence_request_crate",
            "Send a clear photo of today's egg crates", created_by="rule_engine", source="rule_engine",
            assigned_employee_id=employee_id, requires_evidence=True,
            related_inbox_id=inbox_id, related_submission_id=submission_id,
            instructions="Photo proof is required for reported crate production at Nughe Farms Warri.")
        if evidence is not None:
            reminder = await _ensure_reminder(tenant_id, business_id, branch_id, evidence["id"])
            result["reminder_policy_configured"] = reminder["policy_configured"]
        if task is not None:
            queued = await notifier.queue_message(tenant_id, business_id, branch_id, employee_id, task["id"],
                "evidence_request_crate", "Please send a clear photo of today's egg crates.",
                provider_account=phone_number_id)
            result["queued_messages"].append(queued)

    mortality_count = fields.get("mortality_count")
    if mortality_count is not None and mortality_count > 0:
        incident = await _create_mortality_incident(tenant_id, business_id, branch_id, submission_id,
            employee_id, mortality_count, fields.get("suspected_cause"),
            fields.get("reporting_date"))
        result["mortality_incident_id"] = incident["id"] if incident else None
        mortality_evidence = await evidence_store.create_requirement(tenant_id, business_id, branch_id, submission_id,
            employee_id=employee_id)
        if result["evidence_id"] is None:
            result["evidence_id"] = mortality_evidence["id"] if mortality_evidence else None
        if mortality_evidence is not None:
            reminder = await _ensure_reminder(tenant_id, business_id, branch_id, mortality_evidence["id"])
            if result["reminder_policy_configured"] is None:
                result["reminder_policy_configured"] = reminder["policy_configured"]

        mortality_task = await task_engine.create_task(tenant_id, business_id, branch_id,
            "evidence_request_mortality", "Send a clear photo of the reported mortality",
            created_by="rule_engine", source="rule_engine", assigned_employee_id=employee_id,
            requires_evidence=True, related_inbox_id=inbox_id, related_submission_id=submission_id,
            instructions="Photo proof is required for reported mortality at Nughe Farms Warri.")
        if mortality_task is not None:
            queued = await notifier.queue_message(tenant_id, business_id, branch_id, employee_id,
                mortality_task["id"], "evidence_request_mortality",
                "Mortality was reported. Please send a clear photo as evidence.",
                provider_account=phone_number_id)
            result["queued_messages"].append(queued)

        phillip_id = await _resolve_employee_by_name(tenant_id, "Phillip")
        if phillip_id is not None:
            task = await task_engine.create_task(tenant_id, business_id, branch_id, "bird_care_followup",
                "Review reported bird mortality and record observations", created_by="rule_engine",
                source="rule_engine", assigned_employee_id=phillip_id, requires_evidence=False,
                related_inbox_id=inbox_id, related_submission_id=submission_id,
                instructions="%s bird(s) reported dead. Do not diagnose from a photo alone; "
                    "record what you directly observe." % mortality_count)
            result["phillip_task_id"] = task["id"] if task else None
            if incident is not None and task is not None:
                await rest_patch("/rest/v1/biz_mortality_incidents", {"id": "eq." + incident["id"]},
                    {"follow_up_task_id": task["id"]})
            if task is not None:
                # Phillip has no confirmed WhatsApp sender identity yet (see
                # Stage 007B item 6) -- queue_message will record this as
                # status='skipped_no_identity' rather than attempting a send.
                queued = await notifier.queue_message(tenant_id, business_id, branch_id, phillip_id,
                    task["id"], "bird_care_followup",
                    "A bird mortality was reported. Please review and record your observations.",
                    provider_account=phone_number_id)
                result["queued_messages"].append(queued)
    return result


async def _create_mortality_incident(tenant_id, business_id, branch_id, submission_id, reported_by,
        mortality_count, suspected_cause, occurred_on):
    existing = await rest_get("/rest/v1/biz_mortality_incidents", {"submission_id": "eq." + submission_id})
    if existing:
        return existing[0]
    row = {"tenant_id": tenant_id, "business_id": business_id, "branch_id": branch_id,
        "submission_id": submission_id, "reported_by": reported_by, "mortality_count": mortality_count,
        "occurred_on": occurred_on,
        # Cause is stored as NULL/unknown unless the staff message explicitly stated one.
        # YAMSI never infers or diagnoses a cause.
        "suspected_cause": suspected_cause, "cause_source": "staff_reported" if suspected_cause else None}
    await rest_post("/rest/v1/biz_mortality_incidents", [row], prefer="return=minimal")
    rows = await rest_get("/rest/v1/biz_mortality_incidents", {"submission_id": "eq." + submission_id})
    return rows[0] if rows else None


async def escalate_stale_incidents(now=None):
    """Escalates mortality incidents still 'pending' follow-up past each
    business/branch's own owner-confirmed escalation_hours. An incident
    whose business/branch has no configured policy is left alone entirely
    -- never escalated on a fabricated timeout. Not wired to any scheduler
    -- callable manually / from tests only."""
    now = now or datetime.now(timezone.utc)
    pending = await rest_get("/rest/v1/biz_mortality_incidents",
        {"follow_up_status": "eq.pending", "escalated_to_owner": "eq.false"})
    escalated_ids, skipped_unconfigured = [], []
    for incident in pending:
        policy = await get_reminder_policy(incident["tenant_id"], incident["business_id"], incident["branch_id"])
        hours = policy.get("escalation_hours") if policy else None
        if hours is None:
            skipped_unconfigured.append(incident["id"])
            continue
        created_at = datetime.fromisoformat(incident["created_at"].replace("Z", "+00:00"))
        if now - created_at < timedelta(hours=hours):
            continue
        await task_engine.create_task(incident["tenant_id"], incident["business_id"], incident["branch_id"],
            "mortality_escalation", "Unresolved mortality follow-up needs attention",
            created_by="rule_engine", source="rule_engine", priority="high",
            related_submission_id=incident["submission_id"],
            instructions="Mortality incident %s has had no bird-care follow-up for over %s hours." % (incident["id"], hours))
        await rest_patch("/rest/v1/biz_mortality_incidents", {"id": "eq." + incident["id"]},
            {"escalated_to_owner": True, "escalated_at": now.isoformat(), "follow_up_status": "escalated"})
        escalated_ids.append(incident["id"])
    return {"escalated": escalated_ids, "skipped_unconfigured": skipped_unconfigured}


async def get_daily_report_policy(tenant_id, business_id, branch_id):
    """Owner-confirmed {"enabled": bool, "expected_by_hour": int} policy, or
    None if not configured. A missing-report follow-up is only ever created
    when this explicitly says a daily report is expected -- see item 9:
    'missing required report -> follow-up task only if a configured
    reporting rule exists'."""
    rows = await rest_get("/rest/v1/biz_setting_versions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "key": "eq." + DAILY_REPORT_POLICY_KEY, "order": "effective_from.desc", "limit": "1", "select": "value"})
    if not rows:
        return None
    value = rows[0]["value"]
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        return None
    if not value.get("enabled"):
        return None
    return value


async def check_missing_daily_report(tenant_id, business_id, branch_id, employee_id, as_of_date, now=None):
    """No-op unless a daily_reporting_requirement policy is configured and
    enabled for this business/branch -- AI/rule_engine never invents a new
    reporting obligation on its own. When configured, creates (idempotently,
    via dedupe_key) a 'missing_report_followup' task if no
    poultry_daily_report submission exists yet for this employee/date and
    the configured expected_by_hour has passed."""
    policy = await get_daily_report_policy(tenant_id, business_id, branch_id)
    if policy is None:
        return {"applied": False, "reason": "no configured reporting rule"}
    expected_by_hour = policy.get("expected_by_hour")
    now = now or datetime.now(timezone.utc)
    if expected_by_hour is not None and now.hour < expected_by_hour:
        return {"applied": False, "reason": "before expected_by_hour"}
    existing = await rest_get("/rest/v1/biz_submissions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "employee_id": "eq." + employee_id, "kind": "eq.poultry_daily_report",
        "created_at": "gte." + as_of_date, "select": "id", "limit": "1"})
    if existing:
        return {"applied": False, "reason": "report already submitted"}
    dedupe_key = "missing_report:%s:%s:%s:%s" % (business_id, branch_id, employee_id, as_of_date)
    task = await task_engine.create_task(tenant_id, business_id, branch_id, "missing_report_followup",
        "Daily report not yet received", created_by="rule_engine", source="rule_engine",
        assigned_employee_id=employee_id, dedupe_key=dedupe_key,
        instructions="No daily report has been received for %s yet." % as_of_date)
    return {"applied": True, "task_id": task["id"] if task else None}
