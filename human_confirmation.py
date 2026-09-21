"""Phase 3: controlled human confirmation and WhatsApp review workflow.

Sends a CONFIRMABLE biz_submission through the atomic database boundary
created in supabase/migrations/20260918061326_human_confirmation_workflow.sql.
There is deliberately no independent write path here: preview validates and
computes without writing, reference issuance calls exactly one atomic RPC
(amose_issue_review_reference), confirmation calls exactly one atomic RPC
(amose_confirm_submission), and rejection calls exactly one atomic RPC
(amose_reject_submission). Raw receipts and draft creation
(message_processor, whatsapp_webhook) never post operational data and never
call this module.

WhatsApp-facing review uses ONLY the opaque review reference (YR-XXXXXXXXXX
over an unambiguous uppercase alphabet). Internal submission UUIDs are never
accepted in chat commands and never appear in outbound messages: the parser
rejects UUIDs outright, and the database resolves the reference to the
locked submission inside its own tenant. Tenant/business/branch scope,
reviewer authorization (explicit biz_review_authorizations rows, never plain
membership), separation of duties (the reporter cannot review their own
report), and acknowledgement routing (original sender + original inbound
provider-account snapshot) are all enforced inside PostgreSQL.

Submission-kind mapping (explicit; must stay in sync with the migration):

    submission kind          posting type   review RPC
    -----------------------  ------------   --------------------------
    'production'             production     amose_confirm_submission
    'poultry_daily_report'   production     amose_confirm_submission
    'sale'                   sale           amose_confirm_submission
    'payment'                payment        amose_confirm_submission
    'expense'                expense        amose_confirm_submission
    'cash_handover'          cash_handover  amose_confirm_submission
    'bank_deposit'           bank_deposit   amose_confirm_submission
    'stock'                  stock          amose_confirm_submission
    'customer_payment'       customer_payment amose_confirm_submission
    'customer_debt'          customer_debt  amose_confirm_submission
    anything else            --             rejected, no RPC call

Safety properties (enforced, tested in test_human_confirmation.py):

- The entry points accept a review reference (or, for issuance/preview, a
  submission UUID) plus reviewer provider/sender and an explicit structured
  verified snapshot -- never arbitrary SQL, table names, or function names.
  Each entry point has a fixed single-RPC allowlist; any other function
  name raises before any network call.
- Tenant/business/branch scope is never accepted from the caller: RPC
  bodies carry only the review reference (or submission ID for issuance),
  reviewer provider/sender, verified block, request key, and optional
  reason. Scope comes from the locked submission row inside the database.
  Idempotency keys are tenant-scoped in the database, so one tenant can
  never reserve or conflict with another tenant's key.
- preview_submission() performs zero writes (a single read for the
  authoritative kind, then pure validation). It validates required values
  and integer types strictly: only real ints are accepted (bools, floats,
  and numeric strings are all rejected -- nothing dangerous is coerced),
  sale totals are computed from lines, and missing/invalid fields are
  reported without inventing product/customer/employee/sale IDs.
- confirm_submission() performs no submission read of its own: the review
  reference is opaque, so local schema pre-validation applies only when the
  verified snapshot carries a supported kind label; the database always
  re-validates authoritatively, and the response is strictly checked
  (kind/posting-type consistency, echoed reference/key/IDs).
- No caller routing arguments exist: acknowledgement routing is resolved
  authoritatively inside the database, so there is no provider_account or
  destination parameter to misuse.
- Brain memory is created INSIDE the Phase 2 posting transaction invoked by
  the confirm RPC, never by a later independent Python write. This module
  makes no direct operational or Brain writes of any kind.
- Configuration/database unavailability surfaces as WorkflowDatabaseError
  (a WorkflowError AND a DatabaseUnavailable, so callers catching either
  are safe). Malformed RPC responses are rejected, never trusted.
- WhatsApp review commands are parsed by parse_review_command() ONLY and
  accept ONLY explicit commands containing a review reference, never a
  UUID. Casual words ("yes", "ok", "confirm") never parse. The parser and
  execute_review_command() are NOT wired into message_processor or the
  webhook: inbound wiring stays disconnected, so ordinary ingestion still
  creates drafts only.
"""

import re
import uuid as uuid_module
from datetime import date, datetime

import httpx
from supabase_backend import credentials, DatabaseUnavailable

CONFIRM_RPC = "amose_confirm_submission"
REJECT_RPC = "amose_reject_submission"
ISSUE_RPC = "amose_issue_review_reference"
CANCEL_RPC = "amose_cancel_submission"

# Each entry point may call exactly one RPC. Kept as separate singletons so
# a confirmation can never reach the rejection, cancellation, or issuance
# RPC and vice versa.
CONFIRM_ALLOWLIST = frozenset({CONFIRM_RPC})
REJECT_ALLOWLIST = frozenset({REJECT_RPC})
ISSUE_ALLOWLIST = frozenset({ISSUE_RPC})
CANCEL_ALLOWLIST = frozenset({CANCEL_RPC})

# Opaque review reference: YR- plus 10 characters from the unambiguous
# uppercase alphabet (no 0/O, 1/I/L). Mirrors the migration's CHECK.
REVIEW_REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REVIEW_REF_PATTERN = re.compile(r"^YR-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{10}$")

# submission kind -> posting type (mirrors the migration's fixed mapping).
KIND_TO_POSTING = {
    "production": "production",
    "poultry_daily_report": "production",
    "sale": "sale",
    "payment": "payment",
    "expense": "expense",
    "cash_handover": "cash_handover",
    "bank_deposit": "bank_deposit",
    "stock": "stock",
    "customer_payment": "customer_payment",
    "customer_debt": "customer_debt",
}

# posting type -> RPC result IDs that must be present, non-empty strings.
REQUIRED_RESULT_IDS = {
    "production": ("production_run_id", "brain_memory_id"),
    "sale": ("sale_id", "brain_memory_id"),
    "payment": ("payment_id", "brain_memory_id"),
    "expense": ("expense_id", "brain_memory_id"),
    "cash_handover": ("cash_custody_entry_id", "brain_memory_id"),
    "bank_deposit": ("cash_custody_entry_id", "brain_memory_id"),
    "stock": ("stock_count_id", "brain_memory_id"),
    "customer_payment": ("customer_payment_id", "cash_custody_entry_id",
        "brain_memory_id"),
    "customer_debt": ("customer_debt_id", "brain_memory_id"),
}

VALID_REVIEW_ACTIONS = frozenset({"confirmed", "corrected"})
REVIEW_PROVIDERS = frozenset({"whatsapp", "telegram"})
REQUEST_KEY_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")

PRODUCTION_SHIFTS = frozenset({"morning", "afternoon", "night", "full_day"})
SALE_STORAGE_STATES = frozenset({"normal", "cold"})
PAYMENT_METHODS = frozenset({"cash", "transfer", "pos", "credit_adjustment"})
EXPENSE_CATEGORIES = frozenset({"fuel", "maintenance", "salaries", "transport",
    "packaging", "utilities", "rent", "purchases", "other",
    "task_force", "atwap_dues", "tricycle_service"})
EXPENSE_PAYMENT_METHODS = frozenset({"cash", "transfer", "pos", "other"})


class WorkflowError(Exception):
    """Base class: the review was refused or failed; nothing was half-written
    (the RPC is transactional, so a failure means a full rollback)."""


class WorkflowDatabaseError(WorkflowError, DatabaseUnavailable):
    """Database/config failures, including missing credentials: catching
    WorkflowError always catches these too (fail closed), and code that
    already catches DatabaseUnavailable keeps working."""


class ReviewValidationError(WorkflowError):
    """Local argument/verified-snapshot validation failed before any RPC."""


class UnsupportedKindError(WorkflowError):
    """Submission kind has no safe review mapping -- rejected, no records."""


class NotReviewableError(WorkflowError):
    """Submission is not a reviewable draft (or a retry is inconsistent)."""


class ReviewUnauthorizedError(WorkflowError):
    """The reviewer is unknown, inactive, or out of scope."""


class MalformedVerifiedError(WorkflowError):
    """The verified snapshot is missing required fields or has bad values."""


class ScopeMismatchError(WorkflowError):
    """A referenced product/customer/employee/sale/approval is outside the
    submission's authoritative tenant/business/branch scope."""


class ApprovalRequiredError(WorkflowError):
    """A referenced approval request is missing or not approved."""


class SubmissionNotFoundError(WorkflowError):
    pass


class ReviewReferenceError(WorkflowError):
    """The review reference is malformed or unknown (including cross-tenant
    guesses, which read exactly like nonexistent references)."""


class AcknowledgementRoutingError(WorkflowDatabaseError):
    """Authoritative acknowledgement routing is unavailable: the original
    report sender or the original inbound provider-account snapshot is
    unknown, so the review was refused before any state change rather than
    queueing an acknowledgement guaranteed to fail."""


class IncompletePostingError(WorkflowError):
    """Historical state is partial or inconsistent -- refused, fail closed."""


class RequestConflictError(WorkflowError):
    """The request key was reused with different data, or a second distinct
    confirmation was attempted on an already-reviewed submission."""


def _classify_rpc_error(message):
    """Maps the RPC's typed error prefix to a domain exception. Unknown
    database failures stay a WorkflowDatabaseError (fail closed)."""
    if message.startswith("NOT_FOUND:"):
        # Unknown review references (including cross-tenant guesses) and
        # unknown submissions share this prefix; the reference-shaped case
        # maps to ReviewReferenceError so callers can distinguish a bad
        # chat reference from a bad internal ID without learning anything
        # about other tenants.
        if "review reference" in message:
            return ReviewReferenceError(message)
        return SubmissionNotFoundError(message)
    if message.startswith("ROUTING:"):
        return AcknowledgementRoutingError(message)
    if message.startswith("NOT_REVIEWABLE:"):
        return NotReviewableError(message)
    if message.startswith("UNAUTHORIZED:"):
        return ReviewUnauthorizedError(message)
    if message.startswith("UNSUPPORTED_KIND:"):
        return UnsupportedKindError(message)
    if message.startswith("MALFORMED:"):
        return MalformedVerifiedError(message)
    if message.startswith("SCOPE:"):
        return ScopeMismatchError(message)
    if message.startswith("APPROVAL:"):
        return ApprovalRequiredError(message)
    if message.startswith("INCOMPLETE:"):
        return IncompletePostingError(message)
    if message.startswith("CONFLICT:"):
        return RequestConflictError(message)
    return WorkflowDatabaseError(message)


# ---------------------------------------------------------------------------
# Pure validation/preview layer. No network, no writes. Integers are strict:
# only genuine ints pass (bool is rejected explicitly); floats and strings
# -- however numeric-looking -- are rejected rather than coerced.
# ---------------------------------------------------------------------------

def _is_strict_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_uuid(errors, value, field):
    if not isinstance(value, str):
        errors.append("%s must be a UUID string" % field)
        return None
    try:
        uuid_module.UUID(value)
    except (ValueError, AttributeError, TypeError):
        errors.append("%s must be a valid UUID" % field)
        return None
    return value


def _require_int(errors, value, field, minimum=None):
    if not _is_strict_int(value):
        errors.append("%s must be an integer" % field)
        return None
    if minimum is not None and value < minimum:
        errors.append("%s must be >= %d" % (field, minimum))
        return None
    return value


def _optional_uuid(errors, value, field):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _require_uuid(errors, value, field)


def _optional_timestamp(errors, value, field):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        errors.append("%s must be an ISO-8601 timestamp string" % field)
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        errors.append("%s must be an ISO-8601 timestamp string" % field)
        return None
    return value


def posting_for_kind(kind):
    """Returns the posting type for a submission kind, or raises
    UnsupportedKindError without touching the database."""
    try:
        return KIND_TO_POSTING[kind]
    except (KeyError, TypeError):
        raise UnsupportedKindError(
            "Unsupported submission kind: %r -- no review mapping" % (kind,))


def _validate_production(verified, errors):
    _require_uuid(errors, verified.get("product_id"), "product_id")
    _require_int(errors, verified.get("good_quantity"), "good_quantity", minimum=0)
    if "rejected_quantity" in verified:
        _require_int(errors, verified.get("rejected_quantity"),
            "rejected_quantity", minimum=0)
    raw_date = verified.get("production_date")
    if isinstance(raw_date, date) and not isinstance(raw_date, datetime):
        pass
    elif isinstance(raw_date, str):
        try:
            date.fromisoformat(raw_date)
        except ValueError:
            errors.append("production_date must be YYYY-MM-DD")
    else:
        errors.append("production_date must be YYYY-MM-DD")
    if verified.get("shift") not in PRODUCTION_SHIFTS:
        errors.append("shift must be one of %s"
            % (sorted(PRODUCTION_SHIFTS),))
    if "produced_by" in verified:
        _optional_uuid(errors, verified.get("produced_by"), "produced_by")


def _validate_sale(verified, errors):
    computed = {"line_count": 0, "subtotal_kobo": 0, "discount_kobo": 0,
        "total_kobo": 0}
    lines = verified.get("lines")
    if not isinstance(lines, list) or not lines:
        errors.append("lines must be a non-empty array")
        return computed
    subtotal = 0
    for index, line in enumerate(lines, start=1):
        label = "lines[%d]" % index
        if not isinstance(line, dict):
            errors.append("%s must be an object" % label)
            continue
        _require_uuid(errors, line.get("product_id"), label + ".product_id")
        quantity = _require_int(errors, line.get("quantity"),
            label + ".quantity", minimum=1)
        unit_price = _require_int(errors, line.get("unit_price_kobo"),
            label + ".unit_price_kobo", minimum=0)
        storage = line.get("storage_state", "normal")
        if storage not in SALE_STORAGE_STATES:
            errors.append(label + ".storage_state must be one of %s"
                % (sorted(SALE_STORAGE_STATES),))
        if quantity is not None and unit_price is not None:
            subtotal += quantity * unit_price
    computed["line_count"] = len(lines)
    computed["subtotal_kobo"] = subtotal
    if "customer_id" in verified:
        _optional_uuid(errors, verified.get("customer_id"), "customer_id")
    discount = 0
    if "discount_kobo" in verified:
        discount = _require_int(errors, verified.get("discount_kobo"),
            "discount_kobo", minimum=0)
        if discount is None:
            discount = 0
    computed["discount_kobo"] = discount
    if discount > subtotal:
        errors.append("discount_kobo exceeds the computed subtotal")
    computed["total_kobo"] = subtotal - discount
    if "sold_at" in verified:
        _optional_timestamp(errors, verified.get("sold_at"), "sold_at")
    payment = verified.get("payment")
    if payment is not None:
        if not isinstance(payment, dict):
            errors.append("payment must be an object")
        else:
            amount = _require_int(errors, payment.get("amount_kobo"),
                "payment.amount_kobo", minimum=1)
            if payment.get("method") not in PAYMENT_METHODS:
                errors.append("payment.method must be one of %s"
                    % (sorted(PAYMENT_METHODS),))
            if amount is not None and amount > computed["total_kobo"]:
                errors.append("payment.amount_kobo exceeds the computed sale total")
            if "received_by" in payment:
                _optional_uuid(errors, payment.get("received_by"),
                    "payment.received_by")
            if "paid_at" in payment:
                _optional_timestamp(errors, payment.get("paid_at"),
                    "payment.paid_at")
    return computed


def _validate_payment(verified, errors):
    _require_uuid(errors, verified.get("sale_id"), "sale_id")
    _require_int(errors, verified.get("amount_kobo"), "amount_kobo", minimum=1)
    if verified.get("method") not in PAYMENT_METHODS:
        errors.append("method must be one of %s"
            % (sorted(PAYMENT_METHODS),))
    if "received_by" in verified:
        _optional_uuid(errors, verified.get("received_by"), "received_by")
    if "paid_at" in verified:
        _optional_timestamp(errors, verified.get("paid_at"), "paid_at")
    return {"amount_kobo": verified.get("amount_kobo")
        if _is_strict_int(verified.get("amount_kobo")) else None}


def _validate_expense(verified, errors):
    if verified.get("category") not in EXPENSE_CATEGORIES:
        errors.append("category must be one of %s"
            % (sorted(EXPENSE_CATEGORIES),))
    description = verified.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("description must be a non-empty string")
    _require_int(errors, verified.get("amount_kobo"), "amount_kobo", minimum=1)
    method = verified.get("payment_method", "cash")
    if method not in EXPENSE_PAYMENT_METHODS:
        errors.append("payment_method must be one of %s"
            % (sorted(EXPENSE_PAYMENT_METHODS),))
    if "incurred_at" in verified:
        _optional_timestamp(errors, verified.get("incurred_at"), "incurred_at")
    for field in ("paid_by", "recorded_by", "approval_request_id"):
        if field in verified:
            _optional_uuid(errors, verified.get(field), field)
    return {"amount_kobo": verified.get("amount_kobo")
        if _is_strict_int(verified.get("amount_kobo")) else None}


def _validate_cash_handover(verified, errors):
    """Verified handover: both custodians are authoritative employee UUIDs
    (resolved from scoped database records, never from message text), the
    parties differ, and the amount is positive. Returns computed totals."""
    from_id = _require_uuid(errors, verified.get("from_employee_id"),
        "from_employee_id")
    to_id = _require_uuid(errors, verified.get("to_employee_id"),
        "to_employee_id")
    if isinstance(verified.get("from_employee_id"), str) \
            and isinstance(verified.get("to_employee_id"), str) \
            and verified["from_employee_id"] == verified["to_employee_id"]:
        errors.append("from_employee_id and to_employee_id must differ; "
            "self-handover is not allowed")
    _require_int(errors, verified.get("amount_kobo"), "amount_kobo",
        minimum=1)
    if "handed_at" in verified:
        _optional_timestamp(errors, verified.get("handed_at"), "handed_at")
    if "recorded_by" in verified:
        _optional_uuid(errors, verified.get("recorded_by"), "recorded_by")
    return {"amount_kobo": verified.get("amount_kobo")
        if _is_strict_int(verified.get("amount_kobo")) else None}


def _validate_bank_deposit(verified, errors):
    """Verified deposit: the depositor is an authoritative employee UUID,
    and amount, approved destination account, and deposit reference are
    all present. A deposit is never confirmed without a reference."""
    _require_uuid(errors, verified.get("deposited_by"), "deposited_by")
    _require_int(errors, verified.get("amount_kobo"), "amount_kobo",
        minimum=1)
    destination = verified.get("destination_account")
    if not isinstance(destination, str) or not destination.strip():
        errors.append("destination_account must be a non-empty string")
    reference = verified.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        errors.append("reference must be a non-empty string; a deposit is "
            "never confirmed without a reference")
    if "deposited_at" in verified:
        _optional_timestamp(errors, verified.get("deposited_at"),
            "deposited_at")
    if "recorded_by" in verified:
        _optional_uuid(errors, verified.get("recorded_by"), "recorded_by")
    return {"amount_kobo": verified.get("amount_kobo")
        if _is_strict_int(verified.get("amount_kobo")) else None}


def _validate_stock(verified, errors):
    _require_uuid(errors, verified.get("product_id"), "product_id")
    _require_int(errors, verified.get("normal_quantity"),
        "normal_quantity", minimum=0)
    _require_int(errors, verified.get("cold_quantity"),
        "cold_quantity", minimum=0)
    if "counted_at" in verified:
        _optional_timestamp(errors, verified.get("counted_at"), "counted_at")
    for field in ("counted_by", "recorded_by"):
        if field in verified:
            _optional_uuid(errors, verified.get(field), field)
    return {"normal_quantity": verified.get("normal_quantity")
        if _is_strict_int(verified.get("normal_quantity")) else None,
        "cold_quantity": verified.get("cold_quantity")
        if _is_strict_int(verified.get("cold_quantity")) else None}


def _validate_customer_reference(verified, errors):
    """Customer travels as an authoritative UUID when the reviewer knows
    it, or as the staff-written name the database resolves. At least one
    must be present; the name is never coerced into an ID here."""
    if verified.get("customer_id") not in (None, ""):
        _require_uuid(errors, verified.get("customer_id"), "customer_id")
    elif not isinstance(verified.get("customer_name"), str) \
            or not verified.get("customer_name").strip():
        errors.append("customer_id or customer_name is required")
    elif len(verified["customer_name"].strip()) > 120:
        errors.append("customer_name is too long")


def _validate_customer_payment(verified, errors):
    _validate_customer_reference(verified, errors)
    _require_int(errors, verified.get("amount_kobo"), "amount_kobo", minimum=1)
    if verified.get("method") not in PAYMENT_METHODS:
        errors.append("method must be one of %s"
            % (sorted(PAYMENT_METHODS),))
    if "received_by" in verified:
        _optional_uuid(errors, verified.get("received_by"), "received_by")
    if "paid_at" in verified:
        _optional_timestamp(errors, verified.get("paid_at"), "paid_at")
    if "reference" in verified and verified.get("reference") is not None:
        reference = verified.get("reference")
        if not isinstance(reference, str) or len(reference.strip()) > 200:
            errors.append("reference must be a short string")
    return {"amount_kobo": verified.get("amount_kobo")
        if _is_strict_int(verified.get("amount_kobo")) else None}


def _validate_customer_debt(verified, errors):
    _validate_customer_reference(verified, errors)
    _require_int(errors, verified.get("amount_kobo"), "amount_kobo", minimum=1)
    if "incurred_at" in verified:
        _optional_timestamp(errors, verified.get("incurred_at"), "incurred_at")
    if "recorded_by" in verified:
        _optional_uuid(errors, verified.get("recorded_by"), "recorded_by")
    return {"amount_kobo": verified.get("amount_kobo")
        if _is_strict_int(verified.get("amount_kobo")) else None}


def validate_verified(kind, verified):
    """Pure preview validation for one verified snapshot. Returns
    {"kind", "posting_type", "verified" (normalized copy), "computed"} or
    raises UnsupportedKindError / MalformedVerifiedError carrying every
    missing/invalid field found. Never invents IDs and never writes."""
    posting_type = posting_for_kind(kind)
    if not isinstance(verified, dict):
        raise MalformedVerifiedError("verified snapshot must be an object")
    errors = []
    present_kind = verified.get("kind")
    if present_kind not in (None, "") and present_kind != kind:
        errors.append("verified kind %r does not match submission kind %r"
            % (present_kind, kind))
    normalized = dict(verified)
    if posting_type == "production":
        _validate_production(normalized, errors)
        computed = {"good_quantity": normalized.get("good_quantity")
            if _is_strict_int(normalized.get("good_quantity")) else None}
    elif posting_type == "sale":
        computed = _validate_sale(normalized, errors)
        if isinstance(normalized.get("payment"), dict):
            normalized["payment"] = dict(normalized["payment"])
    elif posting_type == "payment":
        computed = _validate_payment(normalized, errors)
    elif posting_type == "expense":
        computed = _validate_expense(normalized, errors)
    elif posting_type == "cash_handover":
        computed = _validate_cash_handover(normalized, errors)
    elif posting_type == "bank_deposit":
        computed = _validate_bank_deposit(normalized, errors)
    elif posting_type == "stock":
        computed = _validate_stock(normalized, errors)
    elif posting_type == "customer_payment":
        computed = _validate_customer_payment(normalized, errors)
    elif posting_type == "customer_debt":
        computed = _validate_customer_debt(normalized, errors)
    else:  # Unreachable: posting_for_kind already allowlisted the kind.
        raise UnsupportedKindError("Unsupported submission kind: %r" % (kind,))
    if errors:
        raise MalformedVerifiedError(
            "verified snapshot is invalid: " + "; ".join(errors))
    return {"kind": kind, "posting_type": posting_type,
        "verified": normalized, "computed": computed}


# ---------------------------------------------------------------------------
# Controlled service layer. Exactly one RPC per action, fixed allowlists,
# strict argument validation, strict response validation. No direct writes
# to submissions, operational, Brain, audit, or outbound tables -- the RPC
# owns every write inside its transaction.
# ---------------------------------------------------------------------------

def _require_uuid_arg(value, field):
    if not isinstance(value, str):
        raise ReviewValidationError("A %s string is required" % field)
    try:
        uuid_module.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ReviewValidationError("A %s string is required" % field)
    return value


def _require_review_ref(value):
    """A WhatsApp-facing review reference (YR-XXXXXXXXXX). Raw UUIDs are
    never valid here: internal IDs must never travel over chat."""
    if not isinstance(value, str) or not REVIEW_REF_PATTERN.match(value.strip()):
        raise ReviewReferenceError(
            "A review reference of the form YR-XXXXXXXXXX is required")
    return value.strip()


def _require_request_key(value):
    if not isinstance(value, str) or not REQUEST_KEY_PATTERN.match(value):
        raise ReviewValidationError(
            "A request key of 1..128 [A-Za-z0-9:_-] characters is required")
    return value


def _require_sender(provider, sender):
    if provider not in REVIEW_PROVIDERS:
        raise ReviewValidationError(
            "Reviewer provider must be one of %s" % (sorted(REVIEW_PROVIDERS),))
    if not isinstance(sender, str) or not sender.strip():
        raise ReviewValidationError("A reviewer sender identity is required")
    return sender.strip()


def _require_reason(value, field, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError(
            "A non-blank %s is required" % field)
    if len(value.strip()) > 2000:
        raise ReviewValidationError("%s is too long" % field)
    return value.strip()


async def _fetch_submission(submission_id):
    """Reads the authoritative submission row (kind + status only). Scope
    fields are intentionally NOT selected: no caller argument may carry
    tenant/business/branch, not even for display."""
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise WorkflowDatabaseError("Supabase is not configured: " + str(exc)) from None
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=8, follow_redirects=False) as client:
            response = await client.get("/rest/v1/biz_submissions", params={
                "id": "eq." + submission_id,
                "select": "id,kind,status",
            })
    except httpx.HTTPError:
        raise WorkflowDatabaseError(
            "Unable to read submission " + submission_id) from None
    if response.status_code != 200:
        raise WorkflowDatabaseError(
            "Unable to read submission " + submission_id)
    rows = response.json()
    if not rows:
        raise SubmissionNotFoundError("Submission not found: " + submission_id)
    return rows[0]


async def _call_workflow_rpc(function_name, allowed, payload):
    """Calls exactly one allowlisted workflow RPC. Any other function name
    raises before any network call is made."""
    if function_name not in allowed:
        raise WorkflowError(
            "Refusing to call non-allowlisted function: " + function_name)
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise WorkflowDatabaseError("Supabase is not configured: " + str(exc)) from None
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=20, follow_redirects=False) as client:
            response = await client.post("/rest/v1/rpc/" + function_name,
                json=payload)
    except httpx.HTTPError:
        raise WorkflowDatabaseError(
            "Review RPC unreachable: " + function_name) from None
    if response.status_code not in (200, 201):
        message = response.text or ("Review RPC failed: " + function_name)
        raise _classify_rpc_error(_extract_rpc_message(response, message))
    try:
        return response.json()
    except ValueError:
        raise WorkflowDatabaseError(
            "Review RPC returned invalid JSON") from None


def _extract_rpc_message(response, fallback):
    """PostgREST surfaces a plpgsql RAISE message in the body's 'message'
    field; fall back to raw text when the shape is unexpected."""
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
    except ValueError:
        pass
    return fallback


async def preview_submission(submission_id, verified):
    """Validates a verified snapshot against the submission's authoritative
    kind WITHOUT writing anything: one read for kind/status, then pure
    validation. Returns the preview (kind, posting type, computed totals).
    Raises NotReviewableError for non-draft submissions."""
    _require_uuid_arg(submission_id, "submission ID")
    if not isinstance(verified, dict):
        raise MalformedVerifiedError("verified snapshot must be an object")
    submission = await _fetch_submission(submission_id)
    if submission.get("status") != "draft":
        raise NotReviewableError(
            "Submission %s has status %r -- only 'draft' submissions can be reviewed"
            % (submission_id, submission.get("status")))
    checked = validate_verified(submission.get("kind"), verified)
    return {"submission_id": submission_id, "kind": checked["kind"],
        "posting_type": checked["posting_type"], "status": "draft",
        "computed": checked["computed"], "verified": checked["verified"]}


async def request_review_reference(submission_id, request_key):
    """Issues (or idempotently re-returns) the single opaque review
    reference for a draft submission through the dedicated issue RPC.
    Terminal submissions cannot receive a reference. This is an internal
    API call: it takes a submission UUID and never touches chat."""
    _require_uuid_arg(submission_id, "submission ID")
    _require_request_key(request_key)
    payload = {"p_submission_id": submission_id,
        "p_request_key": request_key}
    result = await _call_workflow_rpc(ISSUE_RPC, ISSUE_ALLOWLIST, payload)
    _validate_issue_result(result, submission_id, request_key)
    return result


def _locally_checkable_kind(verified):
    """Returns the supported kind label carried by the verified snapshot,
    or None when local schema pre-validation must be deferred to the
    database (which always validates authoritatively)."""
    kind = verified.get("kind") if isinstance(verified, dict) else None
    if isinstance(kind, str) and kind in KIND_TO_POSTING:
        return kind
    return None


async def confirm_submission(review_ref, reviewer_provider,
        reviewer_sender, verified, request_key, correction_reason=None):
    """Atomically confirms and posts a draft submission through the single
    confirm RPC, addressed by its opaque review reference -- never by UUID.
    Scope, authorization, separation of duties, and acknowledgement routing
    come from inside the database, never from these arguments (which carry
    no tenant/business/branch fields, no provider account, and no
    destination at all). Performs no submission read of its own."""
    clean_ref = _require_review_ref(review_ref)
    _require_sender(reviewer_provider, reviewer_sender)
    _require_request_key(request_key)
    if not isinstance(verified, dict):
        raise MalformedVerifiedError("verified snapshot must be an object")
    reason = None
    if correction_reason is not None:
        reason = _require_reason(correction_reason, "correction reason")
    # Local schema pre-validation only when the snapshot declares a
    # supported kind; otherwise the RPC validates authoritatively and the
    # strict response check below still applies. Status is deliberately NOT
    # gated here -- the RPC owns the draft check so identical retries on an
    # already-confirmed submission still return the original result.
    checked_verified = verified
    hinted_kind = _locally_checkable_kind(verified)
    if hinted_kind is not None:
        checked_verified = validate_verified(hinted_kind, verified)["verified"]
    payload = {"p_review_ref": clean_ref,
        "p_reviewer_provider": reviewer_provider,
        "p_reviewer_sender": reviewer_sender.strip(),
        "p_verified": checked_verified,
        "p_request_key": request_key,
        "p_correction_reason": reason}
    result = await _call_workflow_rpc(CONFIRM_RPC, CONFIRM_ALLOWLIST, payload)
    _validate_confirm_result(result, clean_ref, request_key, checked_verified)
    return result


async def reject_submission(review_ref, reviewer_provider,
        reviewer_sender, reason, request_key):
    """Atomically rejects a draft submission through the dedicated reject
    RPC, addressed by its opaque review reference -- never by UUID.
    Creates no operational or Brain records. Performs no submission read."""
    clean_ref = _require_review_ref(review_ref)
    _require_sender(reviewer_provider, reviewer_sender)
    clean_reason = _require_reason(reason, "rejection reason")
    _require_request_key(request_key)
    payload = {"p_review_ref": clean_ref,
        "p_reviewer_provider": reviewer_provider,
        "p_reviewer_sender": reviewer_sender.strip(),
        "p_reason": clean_reason,
        "p_request_key": request_key}
    result = await _call_workflow_rpc(REJECT_RPC, REJECT_ALLOWLIST, payload)
    _validate_reject_result(result, clean_ref, request_key, clean_reason)
    return result


async def execute_review_command(command, reviewer_provider,
        reviewer_sender, verified=None):
    """Runs one already-parsed review command (see parse_review_command)
    through the reference-based review RPCs. The reviewer identity always
    comes from the command sender, never from the parsed text. A confirm
    command requires the reviewer's verified snapshot; a reject command
    carries its reason inline. Still NOT wired into message_processor or
    the webhook -- ordinary ingestion remains draft-only."""
    if not isinstance(command, dict):
        raise ReviewValidationError("A parsed review command is required")
    action = command.get("action")
    if action == "confirm":
        if not isinstance(verified, dict):
            raise MalformedVerifiedError(
                "a verified snapshot is required to confirm")
        return await confirm_submission(command.get("review_ref"),
            reviewer_provider, reviewer_sender, verified,
            command.get("request_key"),
            correction_reason=command.get("reason"))
    if action == "reject":
        return await reject_submission(command.get("review_ref"),
            reviewer_provider, reviewer_sender, command.get("reason"),
            command.get("request_key"))
    raise ReviewValidationError("Unknown review action: %r" % (action,))


def _validate_issue_result(result, submission_id, request_key):
    """Rejects malformed issue-reference responses: wrong shape, unexpected
    status, bad reference format, or mismatched echoed IDs."""
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") not in ("open", "confirmed", "rejected"):
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r" % (result.get("status"),))
    ref = result.get("review_ref")
    if not isinstance(ref, str) or not REVIEW_REF_PATTERN.match(ref):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed review reference")
    try:
        uuid_module.UUID(result.get("submission_id"))
    except (ValueError, AttributeError, TypeError):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed submission ID")
    if result.get("submission_id") != submission_id:
        raise WorkflowDatabaseError(
            "Review RPC returned a different submission ID")
    if result.get("request_key") != request_key:
        raise WorkflowDatabaseError(
            "Review RPC returned a different request key")


def _validate_confirm_result(result, review_ref, request_key, verified_sent):
    """Rejects malformed confirm responses: wrong shape, unexpected status
    or action, unmapped submission kind, kind/posting-type mismatch, bad
    echoed reference/key/IDs, or missing operational IDs. The expected
    posting type is derived from the response's OWN submission kind through
    the fixed mapping (the RPC is authoritative for kind); a local kind
    hint never overrides it."""
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") != "confirmed":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r" % (result.get("status"),))
    if result.get("review_action") not in VALID_REVIEW_ACTIONS:
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected action: %r"
            % (result.get("review_action"),))
    try:
        posting_type = KIND_TO_POSTING[result.get("submission_kind")]
    except (KeyError, TypeError):
        raise WorkflowDatabaseError(
            "Review RPC returned an unmapped submission kind: %r"
            % (result.get("submission_kind"),))
    if result.get("posting_type") != posting_type:
        raise WorkflowDatabaseError(
            "Review RPC posting_type %r does not match its submission kind %r"
            % (result.get("posting_type"), result.get("submission_kind")))
    try:
        uuid_module.UUID(result.get("submission_id"))
    except (ValueError, AttributeError, TypeError):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed submission ID")
    if result.get("review_ref") != review_ref:
        raise WorkflowDatabaseError(
            "Review RPC returned a different review reference")
    if result.get("request_key") != request_key:
        raise WorkflowDatabaseError(
            "Review RPC returned a different request key")
    if result.get("verified_snapshot") != verified_sent:
        raise WorkflowDatabaseError(
            "Review RPC returned a different verified snapshot")
    audit_id = result.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        raise WorkflowDatabaseError(
            "Review RPC result is missing the audit ID")
    for field in REQUIRED_RESULT_IDS[posting_type]:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            raise WorkflowDatabaseError(
                "Review RPC result is missing required ID: " + field)


def _validate_reject_result(result, review_ref, request_key, reason_sent):
    """Rejects malformed reject responses: wrong shape, unexpected status,
    or mismatched echoed reference/key/reason."""
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") != "rejected":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r" % (result.get("status"),))
    if result.get("review_action") != "rejected":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected action: %r"
            % (result.get("review_action"),))
    try:
        uuid_module.UUID(result.get("submission_id"))
    except (ValueError, AttributeError, TypeError):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed submission ID")
    if result.get("review_ref") != review_ref:
        raise WorkflowDatabaseError(
            "Review RPC returned a different review reference")
    if result.get("request_key") != request_key:
        raise WorkflowDatabaseError(
            "Review RPC returned a different request key")
    if result.get("reason") != reason_sent:
        raise WorkflowDatabaseError(
            "Review RPC returned a different rejection reason")
    audit_id = result.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        raise WorkflowDatabaseError(
            "Review RPC result is missing the audit ID")


# ---------------------------------------------------------------------------
# WhatsApp review-command parser. Pure function, no I/O. The grammar is
# deliberately strict -- casual words ("yes", "ok", "confirm") never parse,
# and raw submission UUIDs are NEVER accepted (internal IDs must never
# travel over chat): only the opaque YR- review reference parses.
# NOT wired into ingestion (see module docstring); execute_review_command()
# runs a parsed command through the reference-based RPCs.
# ---------------------------------------------------------------------------

_REVIEW_CONFIRM = re.compile(
    r"^REVIEW\s+CONFIRM\s+(?P<ref>YR-[A-Za-z0-9]{10})\s+KEY\s+(?P<key>[A-Za-z0-9:_-]{1,128})"
    r"(?:\s+CORRECTION\s+(?P<reason>.+))?\s*$", re.IGNORECASE)
_REVIEW_REJECT = re.compile(
    r"^REVIEW\s+REJECT\s+(?P<ref>YR-[A-Za-z0-9]{10})\s+KEY\s+(?P<key>[A-Za-z0-9:_-]{1,128})"
    r"\s+REASON\s+(?P<reason>.+?)\s*$", re.IGNORECASE)


_CORRECTION_PAIR = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]{0,39})=(?:\"([^\"]*)\"|'([^']*)'|(\S+))$")
_CORRECTION_TOKEN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]{0,39}=\"[^\"]*\""
    r"|[A-Za-z_][A-Za-z0-9_]{0,39}='[^']*'"
    r'|"[^"]*"|\'[^\']*\'|\S+')
_CANCEL_COMMAND = re.compile(
    r"^CANCEL\s+(?P<ref>YR-[A-Za-z0-9]{10})(?:\s+(?P<reason>.+))?\s*$",
    re.IGNORECASE)


def _split_correction(rest):
    """Splits CORRECTION text into ({field: value}, reason-or-None).

    field=value tokens (quote-aware, so customer="Emeka Okafor" stays one
    value) become real value corrections applied before posting; every
    other word, plus any text after a `--` separator, stays the free-text
    audit reason. Returns None when a value is blank or overlong -- the
    whole command is then malformed and refused, never half-applied."""
    if rest is None:
        return {}, None
    zone, sep, after = rest.partition("--")
    reason_words = []
    if sep:
        tail = after.strip()
        reason_words.append(tail)
        rest_zone = zone
    else:
        rest_zone = rest
    corrections = {}
    for match in _CORRECTION_TOKEN.finditer(rest_zone):
        token = match.group(0)
        pair = _CORRECTION_PAIR.match(token)
        if pair:
            value = next(
                part for part in pair.groups()[1:] if part is not None)
            value = value.strip()
            if not value or len(value) > 200:
                return None
            if len(corrections) >= 20:
                return None
            corrections[pair.group(1)] = value
        elif "=" in token or sep:
            # A token claiming the k=v shape that is not a valid pair
            # (blank value, bad key), and any non-pair inside a `--`
            # corrections zone, is malformed rather than silently
            # reasoned: a typo must never become an audit note.
            return None
        else:
            reason_words.append(token)
    reason = " ".join(word for word in reason_words if word).strip()
    if len(reason) > 2000:
        return None
    return corrections, (reason or None)


def parse_review_command(text):
    """Parses an explicit review command, or returns None for anything else
    (ordinary reports, casual acknowledgements, UUID-carrying commands,
    malformed commands).

    Grammar (case-insensitive keywords, exact order, review reference only):
      REVIEW CONFIRM <YR-XXXXXXXXXX> KEY <request-key>
        [CORRECTION [field=value ...] [--] [reason]]
      REVIEW REJECT <YR-XXXXXXXXXX> KEY <request-key> REASON <reason>

    CORRECTION field=value pairs are real value corrections applied to the
    draft before posting (unknown fields fail closed in the database);
    remaining words stay the free-text audit reason. Multi-word values
    need quotes (customer="Emeka Okafor") or a `--` reason separator.

    The reference body must use the unambiguous alphabet (no 0/O, 1/I/L);
    anything else -- including a submission UUID in the reference slot --
    never parses.
    """
    if not isinstance(text, str):
        return None
    match = _REVIEW_CONFIRM.match(text.strip())
    if match:
        ref = match.group("ref").upper()
        if not REVIEW_REF_PATTERN.match(ref):
            return None
        raw_reason = match.group("reason")
        if raw_reason is not None:
            raw_reason = raw_reason.strip()
            if not raw_reason or len(raw_reason) > 2000:
                return None
        split = _split_correction(raw_reason)
        if split is None:
            return None
        corrections, reason = split
        return {"action": "confirm",
            "review_ref": ref,
            "request_key": match.group("key"),
            "reason": reason,
            "corrections": corrections}
    match = _REVIEW_REJECT.match(text.strip())
    if match:
        ref = match.group("ref").upper()
        if not REVIEW_REF_PATTERN.match(ref):
            return None
        reason = match.group("reason").strip()
        if not reason or len(reason) > 2000:
            return None
        return {"action": "reject",
            "review_ref": ref,
            "request_key": match.group("key"),
            "reason": reason}
    return None


def parse_cancel_command(text):
    """Parses a submitter cancellation command, or returns None.

    Grammar (case-insensitive keyword, review reference only):
      CANCEL <YR-XXXXXXXXXX> [reason]

    Only the linked reporter can cancel, and only their own pending
    draft -- enforced inside the database, never here. The reference
    body must use the unambiguous alphabet; anything else never parses.
    """
    if not isinstance(text, str):
        return None
    match = _CANCEL_COMMAND.match(text.strip())
    if not match:
        return None
    ref = match.group("ref").upper()
    if not REVIEW_REF_PATTERN.match(ref):
        return None
    reason = match.group("reason")
    if reason is not None:
        reason = reason.strip()
        if len(reason) > 2000:
            return None
        reason = reason or None
    return {"action": "cancel",
        "review_ref": ref,
        "reason": reason}


def _validate_cancel_result(result, review_ref, request_key, reason_sent):
    """Rejects malformed cancel responses: wrong shape, unexpected status
    or action, unmapped submission kind, or mismatched echoed
    reference/key/reason. Cancellation posts nothing, so no operational
    IDs are required -- only the audit ID proving the withdrawal."""
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") != "cancelled":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r" % (result.get("status"),))
    if result.get("review_action") != "cancelled":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected action: %r"
            % (result.get("review_action"),))
    if result.get("submission_kind") not in KIND_TO_POSTING:
        raise WorkflowDatabaseError(
            "Review RPC returned an unmapped submission kind: %r"
            % (result.get("submission_kind"),))
    try:
        uuid_module.UUID(result.get("submission_id"))
    except (ValueError, AttributeError, TypeError):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed submission ID")
    if result.get("review_ref") != review_ref:
        raise WorkflowDatabaseError(
            "Review RPC returned a different review reference")
    if result.get("request_key") != request_key:
        raise WorkflowDatabaseError(
            "Review RPC returned a different request key")
    if result.get("reason") != reason_sent:
        raise WorkflowDatabaseError(
            "Review RPC returned a different cancellation reason")
    audit_id = result.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        raise WorkflowDatabaseError(
            "Review RPC result is missing the audit ID")
