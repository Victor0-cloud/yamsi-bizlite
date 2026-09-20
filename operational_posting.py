"""Phase 2: controlled operational posting layer for AMOSE and the YAMSI Brain.

Converts a CONFIRMED biz_submission into verified operational records plus
contextual Brain memory through ONE atomic PostgreSQL RPC per posting type.
There is deliberately no chain of independent REST inserts here: partial
records are impossible because each RPC is a single database transaction.

Submission-kind mapping (explicit; must stay in sync with
supabase/migrations/20260917202233_operational_posting_layer.sql):

    submission kind          posting type   RPC
    -----------------------  ------------   --------------------------------
    'production'             production     amose_post_production
    'poultry_daily_report'   production     amose_post_production
    'sale'                   sale           amose_post_sale
    'payment'                payment        amose_post_payment
    'expense'                expense        amose_post_expense
    'cash_handover'          cash_handover  amose_post_cash_handover
    'bank_deposit'           bank_deposit   amose_post_bank_deposit
    anything else            --             rejected, no RPC call, no records

'production' is the canonical AMOSE posting kind. 'poultry_daily_report' is
the legacy kind produced by rule_engine.extract() for nughe_farms/warri; it
routes to the same production RPC but still requires the confirmation-time
'verified' block -- raw poultry fields alone are rejected as malformed.

Safety properties (enforced, tested in test_operational_posting.py):

- The entry point accepts a submission ID only -- never arbitrary SQL,
  table names, or function names. The RPC allowlist is a fixed dict; an
  unknown kind or function name raises before any network call.
- Only status='confirmed' submissions post. Draft, pending, rejected,
  voided, malformed, or unsupported submissions raise typed errors and
  create nothing.
- Tenant/business/branch scope comes from the authoritative submission row
  read back from the database -- never from caller arguments (there are
  none) and never from unverified payload fields.
- Brain memory is created INSIDE the same RPC transaction, never by a
  later independent Python write. This module performs exactly one RPC
  call per posting and zero direct writes to operational/Brain tables.
- This module is NOT wired into message_processor or the WhatsApp webhook:
  receipt and draft creation never post operational data. There is no
  tested confirmation boundary to wire into yet; callers invoke
  post_submission() explicitly after human confirmation.
- This layer is intentionally UNREACHABLE end-to-end until a controlled
  human confirmation step exists that writes payload.verified. No such
  writer is created or wired here.
- Configuration/database unavailability surfaces as PostingDatabaseError
  (a PostingError), so callers catching PostingError are safe. Malformed
  RPC responses (wrong shape, wrong posting type, missing IDs, unexpected
  status) are rejected, never trusted.
"""

import httpx
from supabase_backend import credentials, DatabaseUnavailable

# submission kind -> (posting type, allowlisted RPC function name)
KIND_TO_POSTING = {
    "production": ("production", "amose_post_production"),
    "poultry_daily_report": ("production", "amose_post_production"),
    "sale": ("sale", "amose_post_sale"),
    "payment": ("payment", "amose_post_payment"),
    "expense": ("expense", "amose_post_expense"),
    "cash_handover": ("cash_handover", "amose_post_cash_handover"),
    "bank_deposit": ("bank_deposit", "amose_post_bank_deposit"),
}

# The complete allowlist: the only database functions this module may call.
RPC_ALLOWLIST = frozenset({
    "amose_post_production",
    "amose_post_sale",
    "amose_post_payment",
    "amose_post_expense",
    "amose_post_cash_handover",
    "amose_post_bank_deposit",
})

CONFIRMED_STATUS = "confirmed"

# posting type -> RPC result IDs that must be present, non-empty strings.
# A response missing any of these is rejected, never trusted.
REQUIRED_RESULT_IDS = {
    "production": ("production_run_id", "brain_memory_id"),
    "sale": ("sale_id", "brain_memory_id"),
    "payment": ("payment_id", "brain_memory_id"),
    "expense": ("expense_id", "brain_memory_id"),
    "cash_handover": ("cash_custody_entry_id", "brain_memory_id"),
    "bank_deposit": ("cash_custody_entry_id", "brain_memory_id"),
}

VALID_RESULT_STATUSES = frozenset({"posted", "already_posted"})


class PostingError(Exception):
    """Base class: posting was refused or failed; nothing was half-written
    (the RPC is transactional, so a failure means a full rollback)."""


class UnconfirmedSubmissionError(PostingError):
    """Submission is draft/pending/rejected/voided -- never posted."""


class UnsupportedKindError(PostingError):
    """Submission kind has no safe posting mapping -- rejected, no records."""


class MalformedSubmissionError(PostingError):
    """Confirmed submission lacks the verified block or required fields --
    refused rather than guessed."""


class ScopeMismatchError(PostingError):
    """A referenced product/customer/employee/sale/approval is outside the
    submission's authoritative tenant/business/branch scope."""


class ApprovalRequiredError(PostingError):
    """A referenced approval request is missing or not approved."""


class SubmissionNotFoundError(PostingError):
    pass


class IncompletePostingError(PostingError):
    """A prior posting for this submission is incomplete or inconsistent --
    the RPC refused to report success for partial data. Nothing new was
    written by the refused retry."""


class PostingDatabaseError(PostingError):
    """Database/config failures, including missing credentials: catching
    PostingError always catches these too (fail closed)."""


def _classify_rpc_error(message):
    """Maps the RPC's typed error prefix to a domain exception. Unknown
    database failures stay a PostingDatabaseError (fail closed)."""
    if message.startswith("UNCONFIRMED:"):
        return UnconfirmedSubmissionError(message)
    if message.startswith("UNSUPPORTED_KIND:"):
        return UnsupportedKindError(message)
    if message.startswith("MALFORMED:"):
        return MalformedSubmissionError(message)
    if message.startswith("SCOPE:"):
        return ScopeMismatchError(message)
    if message.startswith("APPROVAL:"):
        return ApprovalRequiredError(message)
    if message.startswith("NOT_FOUND:"):
        return SubmissionNotFoundError(message)
    if message.startswith("INCOMPLETE:"):
        return IncompletePostingError(message)
    return PostingDatabaseError(message)


async def _fetch_submission(submission_id):
    """Reads the authoritative submission row (scope + status + kind)."""
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise PostingDatabaseError("Supabase is not configured: " + str(exc)) from None
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=8, follow_redirects=False) as client:
            response = await client.get("/rest/v1/biz_submissions", params={
                "id": "eq." + submission_id,
                "select": "id,tenant_id,business_id,branch_id,employee_id,kind,status",
            })
    except httpx.HTTPError:
        raise PostingDatabaseError("Unable to read submission " + submission_id) from None
    if response.status_code != 200:
        raise PostingDatabaseError("Unable to read submission " + submission_id)
    rows = response.json()
    if not rows:
        raise SubmissionNotFoundError("Submission not found: " + submission_id)
    return rows[0]


async def _call_rpc(function_name, submission_id):
    """Calls exactly one allowlisted RPC with the submission ID only. Any
    other function name raises before any network call is made."""
    if function_name not in RPC_ALLOWLIST:
        raise PostingError("Refusing to call non-allowlisted function: " + function_name)
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise PostingDatabaseError("Supabase is not configured: " + str(exc)) from None
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers,
                timeout=20, follow_redirects=False) as client:
            response = await client.post("/rest/v1/rpc/" + function_name,
                json={"p_submission_id": submission_id})
    except httpx.HTTPError:
        raise PostingDatabaseError("Posting RPC unreachable: " + function_name) from None
    if response.status_code not in (200, 201):
        message = response.text or ("Posting RPC failed: " + function_name)
        raise _classify_rpc_error(_extract_rpc_message(response, message))
    try:
        return response.json()
    except ValueError:
        raise PostingDatabaseError("Posting RPC returned invalid JSON") from None


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


def posting_for_kind(kind):
    """Returns (posting_type, rpc_function) for a submission kind, or raises
    UnsupportedKindError without touching the database."""
    try:
        return KIND_TO_POSTING[kind]
    except (KeyError, TypeError):
        raise UnsupportedKindError(
            "Unsupported submission kind: %r -- no operational records created" % (kind,))


async def post_submission(submission_id):
    """Posts a CONFIRMED submission atomically via its allowlisted RPC.

    Steps: fetch/validate the authoritative row -> allowlist the kind ->
    call exactly one RPC -> return the RPC's structured result dict
    (status, posting_type, created/existing IDs, is_retry). Retries return
    the already-created result; nothing is duplicated.
    """
    if not submission_id or not isinstance(submission_id, str):
        raise PostingError("A submission ID string is required")
    submission = await _fetch_submission(submission_id)
    if submission.get("status") != CONFIRMED_STATUS:
        raise UnconfirmedSubmissionError(
            "Submission %s has status %r -- only '%s' submissions post"
            % (submission_id, submission.get("status"), CONFIRMED_STATUS))
    posting_type, function_name = posting_for_kind(submission.get("kind"))
    result = await _call_rpc(function_name, submission_id)
    _validate_rpc_result(result, posting_type, submission_id)
    return result


def _validate_rpc_result(result, posting_type, submission_id):
    """Rejects malformed RPC responses: wrong shape, unexpected status,
    wrong posting type, echoed submission mismatch, or missing IDs."""
    if not isinstance(result, dict):
        raise PostingDatabaseError("Posting RPC returned a non-object result")
    if result.get("status") not in VALID_RESULT_STATUSES:
        raise PostingDatabaseError(
            "Posting RPC returned unexpected status: %r" % (result.get("status"),))
    if result.get("posting_type") != posting_type:
        raise PostingDatabaseError(
            "Posting RPC posting_type %r does not match submission kind mapping %r"
            % (result.get("posting_type"), posting_type))
    if result.get("submission_id") != submission_id:
        raise PostingDatabaseError("Posting RPC returned a different submission ID")
    for field in REQUIRED_RESULT_IDS[posting_type]:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            raise PostingDatabaseError(
                "Posting RPC result is missing required ID: " + field)
