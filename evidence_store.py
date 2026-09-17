"""Evidence tracking: links WhatsApp media events to the report/task/incident
that required them. Never stores an unassociated image, and never makes
evidence publicly accessible.

fetch_and_store_media() is a real, production-capable implementation of
Meta media id -> authenticated metadata -> authenticated binary download ->
private Supabase Storage upload. It is intentionally NOT called by
handle_incoming_image() or anything else in this codebase yet -- real media
download stays an explicit, separately-approved action (see Stage 007B
report, item N), same posture as notifier.dispatch_queued_message. Tests
mock every HTTP call; no real production media is ever downloaded here.
"""
import os
from datetime import datetime, timezone
import httpx
from supabase_backend import rest_get, rest_post, rest_patch, credentials
import retry_engine

EVIDENCE_STATUSES = ("required", "received", "missing", "reviewed")
EVIDENCE_BUCKET = "yamsi-evidence"
GRAPH_API_VERSION = "v21.0"
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class MediaRetrievalError(Exception):
    pass


def build_storage_path(tenant_id, business_id, branch_id, evidence_id, media_id, mime_type=None):
    """Deterministic, collision-free private object path. evidence_id (a
    UUID unique per evidence row) is the collision guarantee; tenant/business/
    branch segments keep storage isolated exactly like every DB table here."""
    extension = _EXTENSIONS.get(mime_type, "bin")
    return "%s/%s/%s/%s/%s.%s" % (tenant_id, business_id, branch_id, evidence_id, media_id, extension)


def extract_image_metadata(event):
    """Pulls only what Meta's webhook payload already contains -- no network
    call, no token use. Returns None if this isn't an image event."""
    if not isinstance(event, dict) or event.get("type") != "image":
        return None
    image = event.get("image") or {}
    return {
        "whatsapp_message_id": event.get("id"),
        "sender": event.get("from"),
        "timestamp": event.get("timestamp"),
        "media_id": image.get("id"),
        "mime_type": image.get("mime_type"),
        "caption": image.get("caption"),
    }


async def create_requirement(tenant_id, business_id, branch_id, submission_id, *, employee_id=None, provider="whatsapp"):
    """Idempotent: at most one 'submission' evidence row per submission_id.
    employee_id (who the requirement is FOR) is required for safe image
    linkage later -- see find_open_requirements."""
    existing = await get_requirement_for_submission(submission_id)
    if existing is not None:
        return existing
    row = {"tenant_id": tenant_id, "subject_type": "submission", "employee_id": employee_id,
        "submission_business_id": business_id, "submission_branch_id": branch_id,
        "submission_id": submission_id, "provider": provider, "status": "required"}
    await rest_post("/rest/v1/biz_evidence", [row], params=None, prefer="return=minimal")
    return await get_requirement_for_submission(submission_id)


async def get_requirement_for_submission(submission_id):
    rows = await rest_get("/rest/v1/biz_evidence", {"submission_id": "eq." + submission_id, "subject_type": "eq.submission"})
    return rows[0] if rows else None


async def find_open_requirements(tenant_id, employee_id, business_id, branch_id):
    """All still-open (required or missing) evidence rows for this exact
    tenant+employee+business+branch, most recent first. Scoped to the
    specific employee (not just business/branch) so two staff at the same
    branch never get their evidence cross-linked."""
    rows = await rest_get("/rest/v1/biz_evidence", {
        "tenant_id": "eq." + tenant_id, "employee_id": "eq." + employee_id,
        "submission_business_id": "eq." + business_id, "submission_branch_id": "eq." + branch_id,
        "status": "in.(required,missing)", "order": "created_at.desc"})
    return rows


_MORTALITY_CAPTION_HINTS = ("mortality", "died", "dead", "bird")
_CRATE_CAPTION_HINTS = ("crate", "egg")


async def _submission_reason(submission_id):
    """Best-effort signal for what a submission's evidence requirement was
    about, read from that submission's OWN already-parsed fields -- never a
    guess about content, only used to pick between two already-real pending
    requirements."""
    rows = await rest_get("/rest/v1/biz_submissions", {"id": "eq." + submission_id, "select": "payload"})
    if not rows:
        return None
    fields = ((rows[0].get("payload") or {}).get("parsed") or {}).get("fields", {})
    has_mortality = bool(fields.get("mortality_count"))
    has_crates = "crates" in fields
    if has_mortality and not has_crates:
        return "mortality"
    if has_crates and not has_mortality:
        return "crate"
    return None


async def _disambiguate_by_caption(candidates, caption):
    """Returns the single matching candidate if the caption clearly points
    to exactly one of them, else None (meaning: cannot safely tell)."""
    if not caption:
        return None
    lowered = caption.lower()
    wants_mortality = any(hint in lowered for hint in _MORTALITY_CAPTION_HINTS)
    wants_crate = any(hint in lowered for hint in _CRATE_CAPTION_HINTS)
    if wants_mortality == wants_crate:
        return None
    wanted = "mortality" if wants_mortality else "crate"
    matches = [c for c in candidates if await _submission_reason(c["submission_id"]) == wanted]
    return matches[0] if len(matches) == 1 else None


async def handle_incoming_image(tenant_id, business_id, branch_id, inbox_id, event, employee_id=None):
    """Extracts image metadata and links it to the correct open evidence
    requirement for this exact employee, using the caption to disambiguate
    when more than one is open. NEVER guesses: if there's more than one
    plausible candidate and the caption doesn't clearly resolve it, nothing
    is linked and every candidate is returned for human clarification/review
    -- the raw message stays in biz_message_inbox either way, never
    discarded. Idempotent: only patches a row that doesn't already have
    media attached."""
    metadata = extract_image_metadata(event)
    if metadata is None or employee_id is None:
        return {"linked_evidence_id": None, "ambiguous_candidate_ids": []}
    candidates = await find_open_requirements(tenant_id, employee_id, business_id, branch_id)
    if not candidates:
        return {"linked_evidence_id": None, "ambiguous_candidate_ids": []}
    if len(candidates) == 1:
        requirement = candidates[0]
    else:
        requirement = await _disambiguate_by_caption(candidates, metadata.get("caption"))
        if requirement is None:
            return {"linked_evidence_id": None, "ambiguous_candidate_ids": [c["id"] for c in candidates]}
    body = {
        "status": "received",
        "provider_media_id": metadata["media_id"],
        "mime_type": metadata["mime_type"],
        "caption": metadata["caption"],
        "inbox_id": inbox_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    # Conditional patch: only takes effect if this row hasn't already had
    # media attached, making repeated delivery of the same webhook event safe.
    await rest_patch("/rest/v1/biz_evidence",
        {"id": "eq." + requirement["id"], "provider_media_id": "is.null"}, body)
    return {"linked_evidence_id": requirement["id"], "ambiguous_candidate_ids": []}


async def mark_missing(evidence_id):
    await rest_patch("/rest/v1/biz_evidence", {"id": "eq." + evidence_id, "status": "eq.required"}, {"status": "missing"})


async def review(evidence_id, reviewer_employee_id, notes=None):
    body = {"status": "reviewed", "reviewed_by": reviewer_employee_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat()}
    if notes is not None:
        body["notes"] = notes
    await rest_patch("/rest/v1/biz_evidence", {"id": "eq." + evidence_id, "status": "eq.received"}, body)


def _access_token():
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token:
        raise MediaRetrievalError("WhatsApp media retrieval is not configured")
    return token


async def fetch_and_store_media(media_id, storage_path, *, evidence_id=None, tenant_id=None,
        business_id=None, branch_id=None):
    """Meta media id -> authenticated metadata request -> authenticated
    binary download -> private Supabase Storage upload. Returns storage_path
    unchanged on success (never a public URL). Not called automatically by
    anything in this codebase -- see module docstring.

    evidence_id/tenant_id (optional): when given, records retry state
    (public.biz_retry_state) on failure or success -- see retry_engine."""
    try:
        result = await _retrieve_and_upload(media_id, storage_path)
    except MediaRetrievalError as error:
        if evidence_id is not None and tenant_id is not None:
            await retry_engine.record_failure(tenant_id, "media_retrieval", evidence_id, error,
                business_id=business_id, branch_id=branch_id)
        raise
    if evidence_id is not None and tenant_id is not None:
        await retry_engine.record_success(tenant_id, "media_retrieval", evidence_id)
    return result


async def _retrieve_and_upload(media_id, storage_path):
    token = _access_token()
    headers = {"Authorization": "Bearer " + token}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            meta_response = await client.get(
                "https://graph.facebook.com/%s/%s" % (GRAPH_API_VERSION, media_id), headers=headers)
            if meta_response.status_code != 200:
                raise MediaRetrievalError("Failed to fetch WhatsApp media metadata")
            metadata = meta_response.json()
            download_url = metadata.get("url")
            mime_type = metadata.get("mime_type")
            if not download_url:
                raise MediaRetrievalError("WhatsApp media metadata had no download URL")
            binary_response = await client.get(download_url, headers=headers)
            if binary_response.status_code != 200:
                raise MediaRetrievalError("Failed to download WhatsApp media binary")
            content = binary_response.content
    except httpx.HTTPError:
        raise MediaRetrievalError("WhatsApp media retrieval request failed") from None

    url, secret = credentials()
    storage_headers = {"apikey": secret, "Authorization": "Bearer " + secret,
        "Content-Type": mime_type or "application/octet-stream"}
    try:
        async with httpx.AsyncClient(base_url=url, timeout=20) as storage_client:
            upload_response = await storage_client.post(
                "/storage/v1/object/%s/%s" % (EVIDENCE_BUCKET, storage_path),
                headers=storage_headers, content=content)
    except httpx.HTTPError:
        raise MediaRetrievalError("Evidence storage upload request failed") from None
    if upload_response.status_code not in (200, 201):
        raise MediaRetrievalError("Evidence storage upload failed (HTTP %d)" % upload_response.status_code)
    return storage_path
