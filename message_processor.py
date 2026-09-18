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


async def _process_one(client, row, summary):
    inbox_id = row["id"]
    body = row.get("payload") or {}
    event = body.get("event") or {}
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
    if len(candidates) != 1:
        await _mark_inbox(client, inbox_id, "unmatched")
        summary["unmatched"] += 1
        return
    business_id, branch_id = next(iter(candidates))

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

    text = event.get("text", {}).get("body") if event.get("type") == "text" else None
    extraction = rule_engine.extract(business_id, branch_id, text, received_at=row.get("received_at"))
    submission = {
        "tenant_id": tenant_id, "business_id": business_id, "branch_id": branch_id,
        "employee_id": employee_id, "inbox_id": inbox_id, "idempotency_key": inbox_id,
        "kind": extraction["kind"],
        "payload": {"message_text": text, "sender_phone": sender, "parsed": extraction},
        "status": "draft"}
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
