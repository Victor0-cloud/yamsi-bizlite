"""Stage 2: turns raw biz_message_inbox WhatsApp events into pending
biz_submissions rows for human review. Never posts accounting data and
never guesses sender/business/branch identity.

Stage 007 extends this with: business-scoped structured extraction via
rule_engine.extract() (poultry parsing for nughe_farms/warri, unchanged
sale parsing everywhere else), evidence/task/mortality-incident creation
via rule_engine.apply_rules(), and image-event handling via
evidence_store.handle_incoming_image() -- image events are no longer
discarded as "no text".

Phase 4 wires the secure review loop into this batch without weakening
any boundary: strict REVIEW CONFIRM / REVIEW REJECT commands in inbound
text are handled through exactly one atomic review RPC each (sender
identity, explicit authorization, and separation of duties enforced inside
PostgreSQL; the reference -- never a UUID -- resolves the submission);
fresh drafts of reviewable kinds trigger one idempotent review-request
queue call. Ordinary reports still create drafts only. This module never
posts operational data and never writes review tables directly.

Multi-branch senders never get a guessed branch: with more than one
distinct assignment, a report must open with an explicit
"<branch>:" prefix naming one assigned branch (case-insensitive);
otherwise one idempotent branch_clarification row is queued and the event
stays unmatched. REVIEW commands bypass assignment resolution entirely.
"""
import re
import httpx
from supabase_backend import credentials, DatabaseUnavailable, TENANT
import rule_engine
import evidence_store
import retry_engine
import review_service

INBOX_BATCH_LIMIT = 50

_SALE_KEYWORDS = ("sold", "sale")
_CURRENCY_HINTS = {"ngn": "NGN", "naira": "NGN", "₦": "NGN"}
_FULL_SALE = re.compile(
    r"(?i)\bsold\b\s+(?P<quantity>\d+(?:\.\d+)?)\s+(?P<unit>[a-zA-Z]+)\s+(?:at|for|@)\s+(?P<unit_price>\d+(?:\.\d+)?)")
_PARTIAL_SALE = re.compile(r"(?i)\bsold\b\s+(?P<quantity>\d+(?:\.\d+)?)\s+(?P<unit>[a-zA-Z]+)")
_BRANCH_PREFIX = re.compile(r"^\s*([^:]{1,64}?)\s*:\s*(.*)$", re.DOTALL)
CLARIFICATION_MESSAGE_TYPE = "branch_clarification"


def _singularize(unit):
    return unit[:-1] if len(unit) > 1 and unit.lower().endswith("s") else unit


def _detect_currency(text):
    lowered = text.lower()
    for hint, code in _CURRENCY_HINTS.items():
        if hint in lowered:
            return code
    return None


def parse_message(text):
    """Deterministic parser. Only reports fields explicitly present in the
    text; missing information is listed in missing_fields, never invented."""
    if not text or not text.strip():
        return {"intent": None, "fields": {}, "missing_fields": [], "errors": ["Empty or missing message text"]}
    fields = {}
    missing = []
    errors = []
    match = _FULL_SALE.search(text)
    if match:
        intent = "sale"
        fields["quantity"] = float(match.group("quantity"))
        fields["unit"] = _singularize(match.group("unit"))
        fields["unit_price"] = float(match.group("unit_price"))
    else:
        partial = _PARTIAL_SALE.search(text)
        if partial:
            intent = "sale"
            fields["quantity"] = float(partial.group("quantity"))
            fields["unit"] = _singularize(partial.group("unit"))
            missing.append("unit_price")
            errors.append("Sale price not found in message")
        elif any(k in text.lower() for k in _SALE_KEYWORDS):
            intent = "sale"
            missing.extend(["quantity", "unit", "unit_price"])
            errors.append("Recognized a sale-related message but could not extract quantity/unit/price")
        else:
            intent = None
            errors.append("No recognized intent in message")
    if intent == "sale":
        currency = _detect_currency(text)
        if currency:
            fields["currency"] = currency
        else:
            missing.append("currency")
    return {"intent": intent, "fields": fields, "missing_fields": missing, "errors": errors}


async def _get_json(client, path, params):
    response = await client.get(path, params=params)
    if response.status_code != 200:
        raise DatabaseUnavailable("Read failed for " + path)
    return response.json()


async def _mark_inbox(client, inbox_id, status, processing_error=None):
    body = {"status": status}
    if processing_error is not None:
        body["processing_error"] = processing_error[:500]
    response = await client.patch("/rest/v1/biz_message_inbox", params={"id": "eq." + inbox_id},
        json=body, headers={"Prefer": "return=minimal"})
    if response.status_code not in (200, 204):
        raise DatabaseUnavailable("Unable to update inbox status")


async def _create_submission(client, row):
    response = await client.post("/rest/v1/biz_submissions", params={"on_conflict": "inbox_id"},
        headers={"Prefer": "resolution=ignore-duplicates,return=minimal"}, json=[row])
    if response.status_code not in (200, 201, 204):
        raise DatabaseUnavailable("Unable to create submission")


async def _get_submission_id(client, tenant_id, business_id, branch_id, inbox_id):
    rows = await _get_json(client, "/rest/v1/biz_submissions", {
        "tenant_id": "eq." + tenant_id, "business_id": "eq." + business_id, "branch_id": "eq." + branch_id,
        "inbox_id": "eq." + inbox_id, "select": "id"})
    return rows[0]["id"] if rows else None


def _distinct_assignments(rows):
    return {(r["business_id"], r["branch_id"]) for r in rows}


def _split_branch_prefix(text):
    """Splits an optional leading "<branch>:" prefix. Returns
    (prefix, remainder) with both ends stripped; (None, text) when the text
    opens with no prefix. Never raises on non-text input."""
    if not isinstance(text, str):
        return None, None
    match = _BRANCH_PREFIX.match(text)
    if match is None:
        return None, text
    return match.group(1).strip(), match.group(2).strip()


def _match_assignment(candidates, prefix):
    """Resolves a branch prefix to exactly one assigned (business, branch)
    pair, matching branch ids case-insensitively. Returns None unless the
    match is unique -- a prefix naming branches in several businesses, or
    no assigned branch at all, never resolves (no guessing)."""
    if not prefix:
        return None
    wanted = prefix.lower()
    hits = [pair for pair in candidates if pair[1].lower() == wanted]
    return hits[0] if len(hits) == 1 else None


def _resolve_multi_assignment(candidates, text):
    """Resolves scope for a sender holding several assignments. Returns
    ((business_id, branch_id), stripped_text), or None when the message
    carries no usable explicit prefix (missing, invalid, unassigned,
    ambiguous, or empty remainder) -- the caller must then clarify, never
    submit."""
    prefix, remainder = _split_branch_prefix(text)
    if not prefix or not remainder:
        return None
    match = _match_assignment(candidates, prefix)
    if match is None:
        return None
    return match, remainder


def _clarification_text(candidates):
    """Branch-selection request naming only branches assigned to this
    sender, upper-cased for readability (matching stays case-insensitive).
    Deterministic order, length-capped; carries no business data beyond
    the sender's own branch codes."""
    options = sorted({branch for _, branch in candidates if branch})
    shown = ["%s:" % branch.upper() for branch in options]
    if len(shown) == 2:
        joined = "%s or %s" % (shown[0], shown[1])
    elif len(shown) > 2:
        joined = "%s, or %s" % (", ".join(shown[:-1]), shown[-1])
    else:
        joined = "".join(shown)
    return ("Please begin your message with %s so YAMSI knows which branch "
        "to use." % joined)[:500]


async def _request_branch_selection(client, row, tenant_id, employee_id,
        sender, candidates, summary):
    """Queues one idempotent branch_clarification row for an unresolved
    multi-branch message. Idempotency key is per inbox event, so retries
    and concurrent double-processing can never queue a second reply. The
    row carries no business/branch scope (there is none to record)."""
    key = row["id"] + ":" + CLARIFICATION_MESSAGE_TYPE
    existing = await _get_json(client, "/rest/v1/biz_outbound_messages",
        {"idempotency_key": "eq." + key, "select": "id"})
    if existing:
        return False
    response = await client.post("/rest/v1/biz_outbound_messages",
        params={"on_conflict": "idempotency_key"},
        headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
        json=[{"tenant_id": tenant_id, "business_id": None, "branch_id": None,
            "recipient_employee_id": employee_id, "provider": "whatsapp",
            "provider_sender": sender, "provider_account": row.get("provider_account"),
            "related_task_id": None, "message_type": CLARIFICATION_MESSAGE_TYPE,
            "message_text": _clarification_text(candidates),
            "status": "queued", "idempotency_key": key}])
    if response.status_code not in (200, 201, 204):
        raise DatabaseUnavailable("Unable to queue branch clarification")
    summary["clarifications_queued"] += 1
    return True


DELIVERY_RPC = "amose_apply_delivery_status"
DELIVERY_ALLOWLIST = frozenset({DELIVERY_RPC})
DELIVERY_STATES = frozenset({"sent", "delivered", "read", "failed"})


def _sanitize_status_error(errors):
    """Reduces a Meta status errors array to one short classification
    ("<code>:<title>") without storing any payload. Returns None when no
    usable code is present. Never raises on non-list input."""
    if not isinstance(errors, list):
        return None
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            continue
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            return ("%d:%s" % (code, title.strip()))[:140]
        return str(code)
    return None


async def _call_delivery_rpc(client, provider, provider_account,
        provider_message_id, delivery_state, error_code):
    """Applies one delivery state through exactly one allowlisted RPC.
    Unknown message IDs safely report applied=false; anything
    database-shaped raises DatabaseUnavailable for retry."""
    if DELIVERY_RPC not in DELIVERY_ALLOWLIST:
        raise DatabaseUnavailable("Refusing to call non-allowlisted function")
    try:
        response = await client.post("/rest/v1/rpc/" + DELIVERY_RPC, json={
            "p_provider": provider, "p_provider_account": provider_account,
            "p_provider_message_id": provider_message_id,
            "p_delivery_state": delivery_state,
            "p_error_code": error_code})
    except httpx.HTTPError:
        raise DatabaseUnavailable("Delivery RPC unreachable") from None
    if response.status_code not in (200, 201):
        raise DatabaseUnavailable("Delivery RPC refused the request")
    try:
        result = response.json()
    except ValueError:
        raise DatabaseUnavailable("Delivery RPC returned invalid JSON") from None
    if not isinstance(result, dict) or not isinstance(result.get("applied"), bool):
        raise DatabaseUnavailable("Delivery RPC returned an unexpected result")
    return result


async def _process_status(client, row, summary):
    """Handles one Meta delivery-status event: correlates it to its
    outbound row inside the row's own tenant/account scope and records
    the monotonic delivery state. Creates no submission, posts no
    operational data, and never touches human confirmation -- status
    sync is observability only. Unknown or duplicate states are harmless
    (the row is consumed either way); malformed events fail visibly."""
    inbox_id = row["id"]
    body = row.get("payload") or {}
    event = body.get("event") or {}
    provider = row.get("provider") or "whatsapp"
    account = row.get("provider_account")
    message_id = event.get("id")
    state = event.get("status")
    if not isinstance(message_id, str) or not message_id \
            or state not in DELIVERY_STATES \
            or not isinstance(account, str) or not account:
        await _mark_inbox(client, inbox_id, "failed",
            processing_error="Invalid delivery status event")
        summary["failed"] += 1
        return
    await _call_delivery_rpc(client, provider, account, message_id, state,
        _sanitize_status_error(event.get("errors")))
    await _mark_inbox(client, inbox_id, "processed")


async def _process_one(client, row, summary):
    inbox_id = row["id"]
    body = row.get("payload") or {}
    event = body.get("event") or {}
    if body.get("kind") == "status":
        await _process_status(client, row, summary)
        return
    if body.get("kind") != "message":
        await _mark_inbox(client, inbox_id, "processed")
        summary["skipped_non_message"] += 1
        return
    sender = event.get("from")
    if not isinstance(sender, str) or not sender:
        await _mark_inbox(client, inbox_id, "unmatched")
        summary["unmatched"] += 1
        return
    identities = await _get_json(client, "/rest/v1/biz_sender_identities",
        {"provider": "eq.whatsapp", "provider_sender": "eq." + sender, "select": "tenant_id,employee_id"})
    if len(identities) != 1:
        await _mark_inbox(client, inbox_id, "unmatched")
        summary["unmatched"] += 1
        return
    tenant_id, employee_id = identities[0]["tenant_id"], identities[0]["employee_id"]

    # Review-command isolation: any text claiming the reserved REVIEW
    # CONFIRM / REVIEW REJECT namespace is a review-command attempt and
    # must never flow into ordinary report ingestion. A strict command is
    # handled through exactly one atomic RPC (scope, authorization, and
    # separation of duties resolve inside the database from the reference
    # plus this sender identity; reviewers routinely hold several
    # assignments, so no assignment row is needed). Anything else under the
    # reserved prefix -- UUID references, invalid references, missing
    # KEY/REASON, unsupported syntax -- is refused explicitly with zero
    # submissions, zero extraction, and zero writes. Casual words and
    # ordinary reports never claim the prefix and fall through untouched.
    command_text = None
    if event.get("type") == "text":
        candidate = (event.get("text") or {}).get("body")
        if isinstance(candidate, str):
            command_text = candidate
    if review_service.is_reserved_command(command_text):
        command = review_service.parse_review_command(command_text)
        if command is not None:
            await review_service.handle_review_command(
                client, row, sender, command, summary)
        else:
            await review_service.refuse_malformed_command(
                client, row, summary)
        return

    assignments = await _get_json(client, "/rest/v1/biz_assignments",
        {"tenant_id": "eq." + tenant_id, "employee_id": "eq." + employee_id, "select": "business_id,branch_id"})
    candidates = _distinct_assignments(assignments)
    report_text = None
    if len(candidates) == 1:
        business_id, branch_id = next(iter(candidates))
    elif candidates:
        # Several assignments: only an explicit "<branch>:" prefix naming
        # exactly one assigned branch selects scope. Anything else is
        # clarified, never submitted, never guessed.
        raw_text = (event.get("text") or {}).get("body") \
            if event.get("type") == "text" else None
        resolved = _resolve_multi_assignment(candidates, raw_text)
        if resolved is None:
            await _request_branch_selection(
                client, row, tenant_id, employee_id, sender, candidates, summary)
            await _mark_inbox(client, inbox_id, "unmatched")
            summary["unmatched"] += 1
            return
        (business_id, branch_id), report_text = resolved
    else:
        await _mark_inbox(client, inbox_id, "unmatched")
        summary["unmatched"] += 1
        return

    if event.get("type") == "image":
        result = await evidence_store.handle_incoming_image(
            tenant_id, business_id, branch_id, inbox_id, event, employee_id=employee_id)
        await _mark_inbox(client, inbox_id, "processed")
        if result["linked_evidence_id"]:
            summary["images_linked"] += 1
        elif result["ambiguous_candidate_ids"]:
            summary["images_ambiguous"] += 1
        else:
            summary["images_unlinked"] += 1
        return

    text = report_text if report_text is not None else (
        event.get("text", {}).get("body") if event.get("type") == "text" else None)
    extraction = rule_engine.extract(business_id, branch_id, text, received_at=row.get("received_at"))
    submission = {
        "tenant_id": tenant_id, "business_id": business_id, "branch_id": branch_id,
        "employee_id": employee_id, "inbox_id": inbox_id, "idempotency_key": inbox_id,
        "kind": extraction["kind"],
        "payload": {"message_text": text, "sender_phone": sender, "parsed": extraction},
        "status": "draft",
        # Route snapshot bound to the authorized provider-account
        # registry by a database foreign key: the database rejects any
        # submission whose inbound route is unknown, disabled, or scoped
        # to a different business/branch (fail closed, never guessed).
        "provider": row.get("provider"),
        "provider_account": row.get("provider_account")}
    await _create_submission(client, submission)
    submission_id = await _get_submission_id(client, tenant_id, business_id, branch_id, inbox_id)
    if submission_id is not None:
        await rule_engine.apply_rules(tenant_id, business_id, branch_id, employee_id,
            submission_id, extraction, inbox_id=inbox_id)
        # Human review loop: drafts of reviewable kinds get exactly one
        # idempotent review-request queue call (reference issuance plus one
        # WhatsApp request per eligible reviewer, all inside the RPC).
        # Best-effort by design -- the draft already exists, so a queue
        # failure must never break ingestion or lose the report; the same
        # deterministic key re-queues it later without duplicates.
        if extraction.get("kind") in review_service.REVIEWABLE_KINDS:
            try:
                queue_result = await review_service.queue_review_requests(
                    client, submission_id, "queuereq:" + inbox_id)
                queue_status = queue_result.get("status")
                if queue_status == "queued":
                    summary["review_requests_queued"] += 1
                elif queue_status == "already_queued":
                    summary["review_requests_already_queued"] += 1
                elif queue_status == "no_eligible_reviewer":
                    summary["review_requests_no_reviewer"] += 1
                elif queue_status == "reviewer_unroutable":
                    summary["review_requests_unroutable"] += 1
                else:
                    summary["review_requests_failed"] += 1
            except Exception:
                summary["review_requests_failed"] += 1
    await _mark_inbox(client, inbox_id, "processed")
    summary["submitted"] += 1


async def process_inbox_batch(limit=INBOX_BATCH_LIMIT):
    url, key = credentials()
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + key
    summary = {"scanned": 0, "submitted": 0, "unmatched": 0, "skipped_non_message": 0,
        "clarifications_queued": 0,
        "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0,
        "reviews_confirmed": 0, "reviews_rejected": 0, "reviews_refused": 0, "reviews_failed": 0,
        "review_requests_queued": 0, "review_requests_failed": 0,
        "review_requests_already_queued": 0, "review_requests_no_reviewer": 0,
        "review_requests_unroutable": 0}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=8, follow_redirects=False) as client:
        inbox_rows = await _get_json(client, "/rest/v1/biz_message_inbox", {
            "status": "eq.received", "provider": "eq.whatsapp",
            "select": "id,provider,provider_account,payload,received_at", "order": "received_at.asc", "limit": str(limit)})
        for row in inbox_rows:
            summary["scanned"] += 1
            # Isolated per row: one row's failure must not lose or block the
            # rest of the batch, and must not leave the row silently stuck
            # at status='received' with no explanation. The row is marked
            # 'failed' (not re-scanned automatically) rather than retried in
            # a loop, so a single broken message can never spam retries.
            try:
                await _process_one(client, row, summary)
            except Exception as error:
                summary["failed"] += 1
                try:
                    await _mark_inbox(client, row["id"], "failed", processing_error=str(error))
                except DatabaseUnavailable:
                    pass
                try:
                    await retry_engine.record_failure(TENANT, "inbox_processing", row["id"], error)
                except Exception:
                    pass
    return summary
