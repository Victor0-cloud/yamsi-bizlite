"""Phase 4: secure WhatsApp review integration service.

Wires the Phase 3 atomic review boundary into inbound WhatsApp processing
without weakening any of its guarantees:

- Reviewer authorization is managed only through the least-privilege admin
  RPCs (amose_grant_reviewer / amose_revoke_reviewer /
  amose_list_reviewer_authorizations), which live behind the trusted
  administrative boundary (service_role EXECUTE only) and are never
  reachable from chat input. No authorization rows are seeded anywhere.
- After a valid draft submission exists, message_processor asks this module
  to queue review requests through exactly one atomic RPC
  (amose_queue_review_requests). The database issues the opaque YR-
  reference and queues one WhatsApp review_request per eligible reviewer.
  Queue failures never break ingestion: the draft already exists and the
  request can be re-queued idempotently.
- Strict REVIEW CONFIRM / REVIEW REJECT commands are handled here through
  exactly one RPC each (amose_review_confirm_command for confirm,
  amose_reject_submission for reject). Casual words never parse, UUIDs are
  never accepted, sender identity and explicit authorization plus
  separation of duties are enforced inside PostgreSQL, and inbox rows are
  consumed exactly once. Any text claiming the reserved REVIEW CONFIRM /
  REVIEW REJECT prefix that does not parse is refused explicitly with zero
  submissions and zero writes. This module performs zero direct writes to
  submissions, operational, Brain, audit, or outbound tables -- every write
  happens inside the called RPC's transaction.
- Scope discipline: Python never trusts caller-provided tenant, business,
  branch, employee, recipient, or provider-account values. RPC payloads
  carry only the review reference (or submission ID for queue/admin calls),
  the reviewer's provider/sender, keys, reasons, and capability flags.
  All scope resolves from locked rows inside the database.

Intra-package note: names starting with an underscore imported from
human_confirmation are deliberate single-definition reuse of the Phase 3
boundary rules (sender/key/reason validation, RPC error classification,
reject-response validation), not duplication.
"""

import uuid as uuid_module

import httpx

import human_confirmation
from human_confirmation import (
    AcknowledgementRoutingError,
    ApprovalRequiredError,
    IncompletePostingError,
    KIND_TO_POSTING,
    MalformedVerifiedError,
    NotReviewableError,
    RequestConflictError,
    REQUIRED_RESULT_IDS,
    REVIEW_PROVIDERS,
    REVIEW_REF_PATTERN,
    ReviewReferenceError,
    ReviewUnauthorizedError,
    ReviewValidationError,
    ScopeMismatchError,
    SubmissionNotFoundError,
    UnsupportedKindError,
    VALID_REVIEW_ACTIONS,
    WorkflowDatabaseError,
    WorkflowError,
    _classify_rpc_error,
    _extract_rpc_message,
    _require_reason,
    _require_request_key,
    _require_review_ref,
    _require_sender,
    _require_uuid_arg,
    _validate_cancel_result,
    _validate_reject_result,
    parse_cancel_command,
    parse_review_command,
)

GRANT_RPC = "amose_grant_reviewer"
REVOKE_RPC = "amose_revoke_reviewer"
LIST_RPC = "amose_list_reviewer_authorizations"
QUEUE_RPC = "amose_queue_review_requests"
CMD_CONFIRM_RPC = "amose_review_confirm_command"
CANCEL_RPC = "amose_cancel_submission"

# Each entry point may call exactly one RPC. Rejections reuse the Phase 3
# reject RPC (reason travels inline, no new surface needed); submitter
# cancellations use the dedicated cancel RPC (reporter-only, terminal
# status, never a posting).
GRANT_ALLOWLIST = frozenset({GRANT_RPC})
REVOKE_ALLOWLIST = frozenset({REVOKE_RPC})
LIST_ALLOWLIST = frozenset({LIST_RPC})
QUEUE_ALLOWLIST = frozenset({QUEUE_RPC})
CMD_CONFIRM_ALLOWLIST = frozenset({CMD_CONFIRM_RPC})
CMD_REJECT_ALLOWLIST = frozenset({human_confirmation.REJECT_RPC})
CANCEL_ALLOWLIST = frozenset({CANCEL_RPC})

# Submission kinds that require human review (mirrors the Phase 3 mapping).
REVIEWABLE_KINDS = frozenset(human_confirmation.KIND_TO_POSTING)

GRANT_ACTIONS = frozenset({"granted", "updated", "revoked", "already_revoked"})
QUEUE_STATUSES = frozenset({"queued", "already_queued", "no_eligible_reviewer",
    "reviewer_unroutable"})

# Reserved inbound prefixes. Any text beginning with one of these is a
# review-command attempt and must NEVER flow into ordinary report
# ingestion: if it does not parse as a strict command it is refused
# explicitly, with zero submissions and zero writes. Matched on the
# stripped, upper-cased text so leading whitespace and chat-app casing
# cannot smuggle a command past the gate.
RESERVED_COMMAND_PREFIXES = ("REVIEW CONFIRM", "REVIEW REJECT")

REFUSAL_PREFIX_MESSAGE = ("Refused: text begins with the reserved review "
    "prefix but is not a valid REVIEW command; no submission was created")


def is_reserved_command(text):
    """True when the text claims the review-command namespace, whether or
    not it parses. Only the two explicit prefixes count: casual words and
    ordinary reports never match."""
    if not isinstance(text, str):
        return False
    claimed = text.strip().upper()
    return claimed.startswith(RESERVED_COMMAND_PREFIXES)

# RPC failures that are terminal for one inbox row: the row is consumed
# (processed with a processing_error note) rather than retried, so one bad
# command can never spam retries. Everything else database-shaped is
# treated as transient (row marked failed for manual reprocessing).
# AcknowledgementRoutingError is terminal too but handled in its own branch
# below to document the routing decision explicitly.
TERMINAL_REVIEW_ERRORS = (
    ReviewUnauthorizedError,
    ReviewReferenceError,
    ReviewValidationError,
    UnsupportedKindError,
    NotReviewableError,
    MalformedVerifiedError,
    ScopeMismatchError,
    ApprovalRequiredError,
    SubmissionNotFoundError,
    IncompletePostingError,
    RequestConflictError,
)


class AuthorizationAdminError(WorkflowError):
    """An admin authorization call was refused or failed; nothing was
    half-written (each RPC is transactional)."""


class AuthorizationNotFoundError(AuthorizationAdminError):
    """The referenced tenant, business, branch, or employee does not exist
    (or the employee is not active) in the given scope."""


class ReviewServiceError(WorkflowError):
    """A review-queue or chat-command call was refused; terminal for the
    inbox row that carried it."""


def _require_scope_label(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError("A non-blank %s is required" % field)
    if len(value.strip()) > 128:
        raise ReviewValidationError("%s is too long" % field)
    return value.strip()


def _require_actor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError("Actor must be a non-blank string")
    if len(value.strip()) > 200:
        raise ReviewValidationError("Actor is too long")
    return value.strip()


def _require_capability(value, field):
    if not isinstance(value, bool):
        raise ReviewValidationError(
            "%s must be a boolean (got %r)" % (field, value))
    return value


async def _post_own_rpc(function_name, allowed, payload):
    """Calls exactly one allowlisted RPC over a fresh connection (for
    administrative callers that own no batch client)."""
    if function_name not in allowed:
        raise WorkflowError(
            "Refusing to call non-allowlisted function: " + function_name)
    from supabase_backend import credentials, DatabaseUnavailable
    try:
        url, secret = credentials()
    except DatabaseUnavailable as exc:
        raise WorkflowDatabaseError(
            "Supabase is not configured: " + str(exc)) from None
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


async def _post_rpc(client, function_name, allowed, payload):
    """Calls exactly one allowlisted RPC over the caller's batch client.
    Used by the inbound paths so review handling shares the processor's
    connection instead of opening its own."""
    if function_name not in allowed:
        raise WorkflowError(
            "Refusing to call non-allowlisted function: " + function_name)
    try:
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


def _validate_grant_result(result, request_key):
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") != "ok":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r" % (result.get("status"),))
    if result.get("action") not in GRANT_ACTIONS:
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected action: %r"
            % (result.get("action"),))
    grant = result.get("grant")
    if not isinstance(grant, dict):
        raise WorkflowDatabaseError(
            "Review RPC result is missing the grant block")
    for field in ("tenant_id", "employee_id"):
        try:
            uuid_module.UUID(grant.get(field))
        except (ValueError, AttributeError, TypeError):
            raise WorkflowDatabaseError(
                "Review RPC returned a malformed grant %s" % field)
    if not isinstance(grant.get("business_id"), str) \
            or not grant.get("business_id"):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed grant business_id")
    if grant.get("branch_id") is not None \
            and not isinstance(grant.get("branch_id"), str):
        raise WorkflowDatabaseError(
            "Review RPC returned a malformed grant branch_id")
    for field in ("can_confirm", "can_reject"):
        if not isinstance(grant.get(field), bool):
            raise WorkflowDatabaseError(
                "Review RPC returned a malformed grant %s" % field)
    if result.get("request_key") != request_key:
        raise WorkflowDatabaseError(
            "Review RPC returned a different request key")
    if not isinstance(result.get("is_retry"), bool):
        raise WorkflowDatabaseError(
            "Review RPC result is missing the retry flag")


def _validate_list_result(result, tenant_id):
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") != "ok":
        raise WorkflowDatabaseError(
            "Review RPC returned unexpected status: %r" % (result.get("status"),))
    if result.get("tenant_id") != tenant_id:
        raise WorkflowDatabaseError(
            "Review RPC returned a different tenant")
    authorizations = result.get("authorizations")
    if not isinstance(authorizations, list):
        raise WorkflowDatabaseError(
            "Review RPC result is missing the authorization list")
    for entry in authorizations:
        if not isinstance(entry, dict):
            raise WorkflowDatabaseError(
                "Review RPC returned a malformed authorization entry")
        try:
            uuid_module.UUID(entry.get("employee_id"))
        except (ValueError, AttributeError, TypeError):
            raise WorkflowDatabaseError(
                "Review RPC returned a malformed authorization entry")
        if not isinstance(entry.get("business_id"), str) \
                or not isinstance(entry.get("can_confirm"), bool) \
                or not isinstance(entry.get("can_reject"), bool) \
                or not isinstance(entry.get("active"), bool):
            raise WorkflowDatabaseError(
                "Review RPC returned a malformed authorization entry")


def _validate_queue_result(result, submission_id, request_key):
    if not isinstance(result, dict):
        raise WorkflowDatabaseError(
            "Review RPC returned a non-object result")
    if result.get("status") not in QUEUE_STATUSES:
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
    if not isinstance(result.get("notified"), int) \
            or result.get("notified") < 0:
        raise WorkflowDatabaseError(
            "Review RPC result is missing the notified count")
    if not isinstance(result.get("skipped"), list):
        raise WorkflowDatabaseError(
            "Review RPC result is missing the skipped list")
    if not isinstance(result.get("is_retry"), bool):
        raise WorkflowDatabaseError(
            "Review RPC result is missing the retry flag")


def _validate_command_confirm_result(result, review_ref, request_key):
    """Strict validation for chat-confirm responses. The verified block was
    built inside the database (never echoed from here), so instead of an
    echo check we assert it is a well-formed object: a confirm with no
    applied verified facts is rejected, never trusted."""
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
    if not isinstance(result.get("verified_snapshot"), dict):
        raise WorkflowDatabaseError(
            "Review RPC result is missing the applied verified snapshot")
    audit_id = result.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        raise WorkflowDatabaseError(
            "Review RPC result is missing the audit ID")
    for field in REQUIRED_RESULT_IDS[posting_type]:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            raise WorkflowDatabaseError(
                "Review RPC result is missing required ID: " + field)


# ---------------------------------------------------------------------------
# Administrative boundary: reviewer authorization management. Trusted
# callers only (owner tooling); never chat input. Scope values are validated
# locally for shape; existence and membership are enforced by the RPC.
# ---------------------------------------------------------------------------

async def grant_reviewer(tenant_id, business_id, branch_id, employee_id,
        can_confirm, can_reject, request_key, reason=None, actor=None):
    """Grants (or idempotently returns) reviewer authorization through the
    admin RPC. branch_id None means business-wide. Never accepts chat
    input; scope existence is enforced inside the database."""
    _require_uuid_arg(tenant_id, "tenant ID")
    business = _require_scope_label(business_id, "business ID")
    branch = _require_scope_label(branch_id, "branch ID") \
        if branch_id is not None else None
    _require_uuid_arg(employee_id, "employee ID")
    _require_capability(can_confirm, "can_confirm")
    _require_capability(can_reject, "can_reject")
    if not (can_confirm or can_reject):
        raise ReviewValidationError(
            "At least one capability is required")
    _require_request_key(request_key)
    clean_reason = _require_reason(reason, "reason") \
        if reason is not None else None
    clean_actor = _require_actor(actor)
    payload = {"p_tenant_id": tenant_id, "p_business_id": business,
        "p_branch_id": branch, "p_employee_id": employee_id,
        "p_can_confirm": can_confirm, "p_can_reject": can_reject,
        "p_reason": clean_reason, "p_actor": clean_actor,
        "p_request_key": request_key}
    try:
        result = await _post_own_rpc(GRANT_RPC, GRANT_ALLOWLIST, payload)
    except human_confirmation.SubmissionNotFoundError as error:
        raise AuthorizationNotFoundError(str(error)) from None
    except human_confirmation.MalformedVerifiedError as error:
        raise AuthorizationAdminError(str(error)) from None
    _validate_grant_result(result, request_key)
    return result


async def revoke_reviewer(tenant_id, business_id, branch_id, employee_id,
        request_key, reason=None, actor=None):
    """Deactivates the exact-scope grant (history preserved) through the
    admin RPC. Repeats report already_revoked idempotently."""
    _require_uuid_arg(tenant_id, "tenant ID")
    business = _require_scope_label(business_id, "business ID")
    branch = _require_scope_label(branch_id, "branch ID") \
        if branch_id is not None else None
    _require_uuid_arg(employee_id, "employee ID")
    _require_request_key(request_key)
    clean_reason = _require_reason(reason, "reason") \
        if reason is not None else None
    clean_actor = _require_actor(actor)
    payload = {"p_tenant_id": tenant_id, "p_business_id": business,
        "p_branch_id": branch, "p_employee_id": employee_id,
        "p_reason": clean_reason, "p_actor": clean_actor,
        "p_request_key": request_key}
    try:
        result = await _post_own_rpc(REVOKE_RPC, REVOKE_ALLOWLIST, payload)
    except human_confirmation.SubmissionNotFoundError as error:
        raise AuthorizationNotFoundError(str(error)) from None
    except human_confirmation.MalformedVerifiedError as error:
        raise AuthorizationAdminError(str(error)) from None
    _validate_grant_result(result, request_key)
    return result


async def list_reviewers(tenant_id, business_id=None, branch_id=None,
        include_inactive=False):
    """Reads authorization configuration through the restricted list RPC --
    the only read path (no direct table access exists for any role)."""
    _require_uuid_arg(tenant_id, "tenant ID")
    business = _require_scope_label(business_id, "business ID") \
        if business_id is not None else None
    branch = _require_scope_label(branch_id, "branch ID") \
        if branch_id is not None else None
    if not isinstance(include_inactive, bool):
        raise ReviewValidationError("include_inactive must be a boolean")
    payload = {"p_tenant_id": tenant_id, "p_business_id": business,
        "p_branch_id": branch, "p_include_inactive": include_inactive}
    try:
        result = await _post_own_rpc(LIST_RPC, LIST_ALLOWLIST, payload)
    except human_confirmation.SubmissionNotFoundError as error:
        raise AuthorizationNotFoundError(str(error)) from None
    except human_confirmation.MalformedVerifiedError as error:
        raise AuthorizationAdminError(str(error)) from None
    _validate_list_result(result, tenant_id)
    return result


# ---------------------------------------------------------------------------
# Inbound paths: review-request queueing and strict command handling. These
# run on message_processor's batch client; every write happens inside the
# called RPC's transaction.
# ---------------------------------------------------------------------------

async def queue_review_requests(client, submission_id, request_key):
    """Queues WhatsApp review requests for an existing draft submission
    through exactly one atomic RPC. Scope, reference issuance, message
    content, and routing all resolve inside the database."""
    _require_uuid_arg(submission_id, "submission ID")
    _require_request_key(request_key)
    payload = {"p_submission_id": submission_id,
        "p_request_key": request_key}
    try:
        result = await _post_rpc(client, QUEUE_RPC, QUEUE_ALLOWLIST, payload)
    except human_confirmation.SubmissionNotFoundError as error:
        raise ReviewServiceError(str(error)) from None
    except human_confirmation.MalformedVerifiedError as error:
        raise ReviewServiceError(str(error)) from None
    _validate_queue_result(result, submission_id, request_key)
    return result


def _require_corrections(value):
    """Validates a parsed corrections mapping: plain string field/value
    pairs only (the database allowlists per-kind fields and fails closed
    on anything unknown)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReviewValidationError("Corrections must be field=value pairs")
    if len(value) > 20:
        raise ReviewValidationError("Too many corrections")
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() \
                or len(key.strip()) > 40:
            raise ReviewValidationError("Correction field names are invalid")
        if not isinstance(item, str) or not item.strip() \
                or len(item.strip()) > 200:
            raise ReviewValidationError(
                "Correction values must be short strings")
    return {key.strip(): item.strip() for key, item in value.items()}


async def confirm_from_chat(client, review_ref, reviewer_provider,
        reviewer_sender, request_key, correction_reason=None,
        corrections=None):
    """Runs a strict REVIEW CONFIRM command through exactly one atomic RPC.
    The verified block is built inside the database from the submission's
    own parsed extraction -- never supplied, and never suppliable, here.
    CORRECTION field=value pairs are real value overrides applied to the
    draft before posting (unknown fields fail closed in the database)."""
    clean_ref = _require_review_ref(review_ref)
    _require_sender(reviewer_provider, reviewer_sender)
    _require_request_key(request_key)
    reason = None
    if correction_reason is not None:
        reason = _require_reason(correction_reason, "correction reason")
    clean_corrections = _require_corrections(corrections)
    payload = {"p_review_ref": clean_ref,
        "p_reviewer_provider": reviewer_provider,
        "p_reviewer_sender": reviewer_sender.strip(),
        "p_request_key": request_key,
        "p_correction_reason": reason,
        "p_corrections": clean_corrections}
    result = await _post_rpc(
        client, CMD_CONFIRM_RPC, CMD_CONFIRM_ALLOWLIST, payload)
    _validate_command_confirm_result(result, clean_ref, request_key)
    return result


async def cancel_from_chat(client, review_ref, requester_provider,
        requester_sender, request_key, reason=None):
    """Runs a submitter CANCEL command through exactly one atomic RPC.
    Only the linked reporter can withdraw their own pending draft; the
    database refuses anyone else. Cancellation posts nothing."""
    clean_ref = _require_review_ref(review_ref)
    _require_sender(requester_provider, requester_sender)
    _require_request_key(request_key)
    clean_reason = _require_reason(reason, "cancellation reason") \
        if reason is not None else None
    payload = {"p_review_ref": clean_ref,
        "p_requester_provider": requester_provider,
        "p_requester_sender": requester_sender.strip(),
        "p_request_key": request_key,
        "p_reason": clean_reason}
    result = await _post_rpc(client, CANCEL_RPC, CANCEL_ALLOWLIST, payload)
    _validate_cancel_result(result, clean_ref, request_key, clean_reason)
    return result


async def reject_from_chat(client, review_ref, reviewer_provider,
        reviewer_sender, reason, request_key):
    """Runs a strict REVIEW REJECT command through exactly one atomic RPC
    (the Phase 3 reject RPC -- the reason travels inline)."""
    clean_ref = _require_review_ref(review_ref)
    _require_sender(reviewer_provider, reviewer_sender)
    clean_reason = _require_reason(reason, "rejection reason")
    _require_request_key(request_key)
    payload = {"p_review_ref": clean_ref,
        "p_reviewer_provider": reviewer_provider,
        "p_reviewer_sender": reviewer_sender.strip(),
        "p_reason": clean_reason,
        "p_request_key": request_key}
    result = await _post_rpc(
        client, human_confirmation.REJECT_RPC, CMD_REJECT_ALLOWLIST, payload)
    _validate_reject_result(result, clean_ref, request_key, clean_reason)
    return result


async def _set_inbox_status(client, inbox_id, status, processing_error=None):
    body = {"status": status}
    if processing_error is not None:
        body["processing_error"] = processing_error[:500]
    response = await client.patch("/rest/v1/biz_message_inbox",
        params={"id": "eq." + inbox_id}, json=body,
        headers={"Prefer": "return=minimal"})
    if response.status_code not in (200, 204):
        raise WorkflowDatabaseError("Unable to update inbox status")


async def refuse_malformed_command(client, row, summary):
    """Records an explicit safe refusal for a reserved-prefix text that did
    not parse as a strict command. No submission is created, no extraction
    runs, no RPC is called, and no operational or Brain data is posted: the
    inbox row is consumed with a generic refusal note. The note reveals
    nothing about any tenant's references because no database lookup for
    the text happens at all."""
    await _set_inbox_status(client, row["id"], "processed",
        processing_error="RefusedReviewCommand: " + REFUSAL_PREFIX_MESSAGE)
    summary["reviews_refused"] += 1
    return {"outcome": "refused", "error": "RefusedReviewCommand"}


async def handle_review_command(client, row, sender, command, summary):
    """Handles one already-parsed review command found in inbound text.

    The reviewer identity always comes from the message sender, never from
    the parsed text. Terminal refusals consume the row (processed with a
    processing_error note) so one bad command can never spam retries;
    transient database failures mark the row failed for manual
    reprocessing. Returns an outcome dict; never raises terminal review
    errors (unexpected bugs still propagate to the batch guard)."""
    inbox_id = row["id"]
    provider = row.get("provider") or "whatsapp"
    if provider not in REVIEW_PROVIDERS:
        raise ReviewValidationError(
            "Unsupported review provider: %r" % (provider,))
    action = command.get("action")
    try:
        if action == "confirm":
            result = await confirm_from_chat(
                client, command.get("review_ref"), provider, sender,
                command.get("request_key"),
                correction_reason=command.get("reason"),
                corrections=command.get("corrections"))
            summary["reviews_confirmed"] += 1
        elif action == "reject":
            result = await reject_from_chat(
                client, command.get("review_ref"), provider, sender,
                command.get("reason"), command.get("request_key"))
            summary["reviews_rejected"] += 1
        else:
            raise ReviewValidationError(
                "Unknown review action: %r" % (action,))
    except TERMINAL_REVIEW_ERRORS as error:
        await _set_inbox_status(client, inbox_id, "processed",
            processing_error="%s: %s" % (type(error).__name__, error))
        summary["reviews_refused"] += 1
        return {"outcome": "refused", "error": type(error).__name__}
    except AcknowledgementRoutingError as error:
        # Deterministic configuration gap (no verified sender identity or
        # provider-account snapshot): retrying cannot heal it.
        await _set_inbox_status(client, inbox_id, "processed",
            processing_error="%s: %s" % (type(error).__name__, error))
        summary["reviews_refused"] += 1
        return {"outcome": "refused", "error": type(error).__name__}
    except WorkflowDatabaseError as error:
        await _set_inbox_status(client, inbox_id, "failed",
            processing_error="%s: %s" % (type(error).__name__, error))
        summary["reviews_failed"] += 1
        return {"outcome": "failed", "error": type(error).__name__}
    await _set_inbox_status(client, inbox_id, "processed")
    return {"outcome": action, "result": result}
