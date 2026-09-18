"""Phase 3 tests: the controlled human confirmation workflow.

Unit tests use the same mocking conventions as the rest of the suite
(patch httpx.AsyncClient with an AsyncMock client; real httpx.Response
objects for payloads). No test touches a live database and no test invents
business IDs beyond local throwaway fixtures.

WhatsApp-facing review is addressed ONLY by the opaque review reference
(YR-XXXXXXXXXX). Submission UUIDs appear solely in internal calls
(preview/issue); the parser, confirm, reject, and command-dispatcher paths
never accept a UUID. Database-enforced properties (explicit authorization,
separation of duties, tenant-scoped idempotency, authoritative routing,
reference lifecycle) are additionally covered by transactional SQL probes
(see /tmp phase-3 probes, run against local Supabase when available).
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx

import human_confirmation
from human_confirmation import (
    AcknowledgementRoutingError,
    ApprovalRequiredError,
    IncompletePostingError,
    MalformedVerifiedError,
    NotReviewableError,
    RequestConflictError,
    ReviewReferenceError,
    ReviewUnauthorizedError,
    ReviewValidationError,
    ScopeMismatchError,
    SubmissionNotFoundError,
    UnsupportedKindError,
    WorkflowDatabaseError,
    WorkflowError,
    confirm_submission,
    execute_review_command,
    parse_review_command,
    posting_for_kind,
    preview_submission,
    reject_submission,
    request_review_reference,
    validate_verified,
)
from supabase_backend import DatabaseUnavailable

SUBMISSION_ID = "11111111-1111-1111-1111-111111111111"
PRODUCT_ID = "22222222-2222-2222-2222-222222222222"
APPROVAL_ID = "44444444-4444-4444-4444-444444444444"
REQUEST_KEY = "review-2026-09-18-001"
REVIEW_REF = "YR-ABCD234EFG"
REVIEW_REF_2 = "YR-WXYZ5678HJ"


def _production_verified(**overrides):
    verified = {"product_id": PRODUCT_ID, "good_quantity": 120,
        "rejected_quantity": 3, "production_date": "2026-09-17",
        "shift": "morning"}
    verified.update(overrides)
    return verified


def _sale_verified(**overrides):
    verified = {"lines": [
        {"product_id": PRODUCT_ID, "quantity": 10, "unit_price_kobo": 50000},
        {"product_id": PRODUCT_ID, "quantity": 2, "unit_price_kobo": 45000,
            "storage_state": "cold"}]}
    verified.update(overrides)
    return verified


def _payment_verified(**overrides):
    verified = {"sale_id": "33333333-3333-3333-3333-333333333333",
        "amount_kobo": 100000, "method": "cash"}
    verified.update(overrides)
    return verified


def _expense_verified(**overrides):
    verified = {"category": "fuel", "description": "Diesel for generator",
        "amount_kobo": 75000, "payment_method": "cash"}
    verified.update(overrides)
    return verified


def _submission_row(kind="sale", status="draft"):
    return {"id": SUBMISSION_ID, "kind": kind, "status": status}


def _client(get_rows=None, post_response=None, get_status=200):
    client = AsyncMock()
    client.get = AsyncMock(return_value=httpx.Response(get_status,
        json=get_rows if get_rows is not None else []))
    client.post = AsyncMock(return_value=post_response)
    client.patch = AsyncMock(return_value=httpx.Response(204))
    return client


def _run_with_client(client, coro):
    with patch("human_confirmation.credentials",
               return_value=("https://example.supabase.co", "test-key")), \
         patch("human_confirmation.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        return asyncio.run(coro), client


def _ok_confirm_result(posting_type="sale", request_key=REQUEST_KEY,
        review_ref=REVIEW_REF, verified=None, action="confirmed"):
    body = {"status": "confirmed", "review_action": action,
        "posting_type": posting_type, "submission_kind": "sale",
        "submission_id": SUBMISSION_ID, "review_ref": review_ref,
        "request_key": request_key,
        "verified_snapshot": verified if verified is not None else _sale_verified(),
        "audit_id": "audit-1", "is_retry": False,
        "brain_memory_id": "mem-1"}
    body.update({"sale": {"sale_id": "sale-1"},
        "production": {"production_run_id": "run-1"},
        "payment": {"payment_id": "pay-1"},
        "expense": {"expense_id": "exp-1"}}[posting_type])
    if posting_type == "sale":
        body["submission_kind"] = "sale"
    elif posting_type == "production":
        body["submission_kind"] = "production"
        body["verified_snapshot"] = verified if verified is not None \
            else _production_verified()
    elif posting_type == "payment":
        body["submission_kind"] = "payment"
        body["verified_snapshot"] = verified if verified is not None \
            else _payment_verified()
    else:
        body["submission_kind"] = "expense"
        body["verified_snapshot"] = verified if verified is not None \
            else _expense_verified()
    return httpx.Response(200, json=body)


def _ok_reject_result(request_key=REQUEST_KEY, review_ref=REVIEW_REF,
        reason="duplicate report"):
    return httpx.Response(200, json={"status": "rejected",
        "review_action": "rejected", "submission_kind": "sale",
        "submission_id": SUBMISSION_ID, "review_ref": review_ref,
        "request_key": request_key,
        "reason": reason, "audit_id": "audit-9",
        "is_retry": False})


def _ok_issue_result(request_key=REQUEST_KEY, review_ref=REVIEW_REF,
        status="open"):
    return httpx.Response(200, json={"status": status,
        "review_ref": review_ref, "submission_id": SUBMISSION_ID,
        "submission_kind": "sale", "request_key": request_key,
        "is_retry": status != "open"})


class KindMappingTests(unittest.TestCase):
    def test_supported_kinds_map_to_posting_types(self):
        self.assertEqual(posting_for_kind("production"), "production")
        self.assertEqual(posting_for_kind("poultry_daily_report"), "production")
        self.assertEqual(posting_for_kind("sale"), "sale")
        self.assertEqual(posting_for_kind("payment"), "payment")
        self.assertEqual(posting_for_kind("expense"), "expense")

    def test_unsupported_kind_rejected_without_database(self):
        for kind in ("whatsapp_message", "poultry_report_v9", None, "",
                "DROP TABLE biz_sales;"):
            with self.assertRaises(UnsupportedKindError):
                posting_for_kind(kind)

    def test_no_arbitrary_function_name_accepted(self):
        client = _client()
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(WorkflowError):
                asyncio.run(human_confirmation._call_workflow_rpc(
                    "amose_post_sale; DROP TABLE x",
                    human_confirmation.CONFIRM_ALLOWLIST, {}))
            with self.assertRaises(WorkflowError):
                asyncio.run(human_confirmation._call_workflow_rpc(
                    "public.amose_confirm_submission",
                    human_confirmation.CONFIRM_ALLOWLIST, {}))
            with self.assertRaises(WorkflowError):
                asyncio.run(human_confirmation._call_workflow_rpc(
                    human_confirmation.REJECT_RPC,
                    human_confirmation.CONFIRM_ALLOWLIST, {}))
            with self.assertRaises(WorkflowError):
                asyncio.run(human_confirmation._call_workflow_rpc(
                    human_confirmation.CONFIRM_RPC,
                    human_confirmation.REJECT_ALLOWLIST, {}))
            with self.assertRaises(WorkflowError):
                asyncio.run(human_confirmation._call_workflow_rpc(
                    human_confirmation.ISSUE_RPC,
                    human_confirmation.CONFIRM_ALLOWLIST, {}))
            with self.assertRaises(WorkflowError):
                asyncio.run(human_confirmation._call_workflow_rpc(
                    human_confirmation.CONFIRM_RPC,
                    human_confirmation.ISSUE_ALLOWLIST, {}))
        client.post.assert_not_called()


class PreviewValidationTests(unittest.TestCase):
    def test_production_required_fields(self):
        checked = validate_verified("production", _production_verified())
        self.assertEqual(checked["posting_type"], "production")
        self.assertEqual(checked["computed"]["good_quantity"], 120)

    def test_poultry_report_uses_production_schema(self):
        checked = validate_verified(
            "poultry_daily_report", _production_verified())
        self.assertEqual(checked["posting_type"], "production")

    def test_sale_required_fields_and_totals(self):
        checked = validate_verified("sale", _sale_verified())
        self.assertEqual(checked["computed"]["line_count"], 2)
        self.assertEqual(checked["computed"]["subtotal_kobo"],
            10 * 50000 + 2 * 45000)
        self.assertEqual(checked["computed"]["discount_kobo"], 0)
        self.assertEqual(checked["computed"]["total_kobo"],
            10 * 50000 + 2 * 45000)

    def test_sale_discount_computed(self):
        checked = validate_verified("sale",
            _sale_verified(discount_kobo=90000))
        self.assertEqual(checked["computed"]["total_kobo"],
            10 * 50000 + 2 * 45000 - 90000)

    def test_payment_required_fields(self):
        checked = validate_verified("payment", _payment_verified())
        self.assertEqual(checked["posting_type"], "payment")

    def test_expense_required_fields(self):
        checked = validate_verified("expense", _expense_verified())
        self.assertEqual(checked["posting_type"], "expense")

    def test_unsupported_kind_rejected(self):
        with self.assertRaises(UnsupportedKindError):
            validate_verified("whatsapp_message", {"lines": []})

    def test_non_object_snapshot_rejected(self):
        for bad in (None, [], "verified", 42):
            with self.assertRaises(MalformedVerifiedError):
                validate_verified("sale", bad)

    def test_missing_fields_reported(self):
        with self.assertRaises(MalformedVerifiedError) as ctx:
            validate_verified("production", {"product_id": PRODUCT_ID})
        message = str(ctx.exception)
        self.assertIn("good_quantity", message)
        self.assertIn("production_date", message)
        self.assertIn("shift", message)

    def test_mismatched_kind_label_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("sale",
                _sale_verified(kind="payment"))

    def test_matching_kind_label_accepted(self):
        checked = validate_verified("sale", _sale_verified(kind="sale"))
        self.assertEqual(checked["posting_type"], "sale")


class DangerousCoercionTests(unittest.TestCase):
    def test_float_quantities_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("sale", {"lines": [
                {"product_id": PRODUCT_ID, "quantity": 10.5,
                    "unit_price_kobo": 50000}]})
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("production",
                _production_verified(good_quantity=12.0))

    def test_string_amounts_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("sale", {"lines": [
                {"product_id": PRODUCT_ID, "quantity": "10",
                    "unit_price_kobo": 50000}]})
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("payment",
                _payment_verified(amount_kobo="100000"))
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("expense",
                _expense_verified(amount_kobo="75000"))
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("production",
                _production_verified(good_quantity="120 bags"))

    def test_bool_amounts_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("sale", {"lines": [
                {"product_id": PRODUCT_ID, "quantity": True,
                    "unit_price_kobo": 50000}]})
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("expense", _expense_verified(amount_kobo=True))

    def test_garbage_uuids_rejected(self):
        for bad in ("not-a-uuid", "", 123, None, "ffffffff"):
            with self.assertRaises(MalformedVerifiedError):
                validate_verified("production",
                    _production_verified(product_id=bad))

    def test_no_ids_invented_for_missing_fields(self):
        """A snapshot missing its IDs fails with a missing-field error --
        validation never fills anything in."""
        with self.assertRaises(MalformedVerifiedError) as ctx:
            validate_verified("payment",
                {"amount_kobo": 100, "method": "cash"})
        self.assertIn("sale_id", str(ctx.exception))

    def test_bad_enums_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("production",
                _production_verified(shift="whenever"))
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("payment",
                _payment_verified(method="barter"))
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("expense",
                _expense_verified(category="yacht"))
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("expense",
                _expense_verified(description="   "))

    def test_discount_above_subtotal_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("sale", _sale_verified(discount_kobo=10 ** 12))

    def test_embedded_overpayment_rejected(self):
        verified = _sale_verified(payment={"amount_kobo": 10 ** 12,
            "method": "cash"})
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("sale", verified)

    def test_bad_dates_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("production",
                _production_verified(production_date="17/09/2026"))
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("payment",
                _payment_verified(paid_at="yesterday"))


class PreviewNoWriteTests(unittest.TestCase):
    def test_preview_performs_no_writes(self):
        client = _client(get_rows=[_submission_row()],
            post_response=_ok_confirm_result())
        result, client = _run_with_client(client,
            preview_submission(SUBMISSION_ID, _sale_verified()))
        self.assertEqual(result["posting_type"], "sale")
        self.assertEqual(result["computed"]["total_kobo"],
            10 * 50000 + 2 * 45000)
        client.post.assert_not_called()
        client.patch.assert_not_called()
        client.get.assert_called_once()

    def test_preview_rejects_non_draft_without_writes(self):
        for status in ("confirmed", "rejected"):
            client = _client(get_rows=[_submission_row(status=status)],
                post_response=_ok_confirm_result())
            with self.assertRaises(NotReviewableError, msg=status):
                _run_with_client(client,
                    preview_submission(SUBMISSION_ID, _sale_verified()))
            client.post.assert_not_called()
            client.patch.assert_not_called()

    def test_preview_missing_submission(self):
        client = _client(get_rows=[], post_response=_ok_confirm_result())
        with self.assertRaises(SubmissionNotFoundError):
            _run_with_client(client,
                preview_submission(SUBMISSION_ID, _sale_verified()))
        client.post.assert_not_called()

    def test_preview_uses_authoritative_kind(self):
        """The kind comes from the database row, not from the snapshot."""
        client = _client(get_rows=[_submission_row(kind="payment")],
            post_response=_ok_confirm_result())
        with self.assertRaises(MalformedVerifiedError):
            _run_with_client(client,
                preview_submission(SUBMISSION_ID, _sale_verified()))

    def test_preview_bad_ids_rejected_locally(self):
        client = _client()
        for bad in ("", None, 123, "not-a-uuid"):
            with self.assertRaises(WorkflowError):
                asyncio.run(preview_submission(bad, _sale_verified()))
        client.post.assert_not_called()


class IssueReferenceTests(unittest.TestCase):
    def test_issue_calls_only_the_fixed_issue_rpc(self):
        client = _client(post_response=_ok_issue_result())
        result, client = _run_with_client(client,
            request_review_reference(SUBMISSION_ID, REQUEST_KEY))
        self.assertEqual(result["review_ref"], REVIEW_REF)
        self.assertEqual(result["status"], "open")
        client.post.assert_called_once()
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_issue_review_reference")

    def test_issue_payload_carries_no_scope_or_routing(self):
        client = _client(post_response=_ok_issue_result())
        _result, client = _run_with_client(client,
            request_review_reference(SUBMISSION_ID, REQUEST_KEY))
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(set(body), {"p_submission_id", "p_request_key"})
        for forbidden in ("tenant_id", "business_id", "branch_id",
                "p_review_ref", "review_ref", "p_provider_account"):
            self.assertNotIn(forbidden, body)

    def test_issue_performs_no_reads_or_writes(self):
        """Issuance is one RPC: no GET, no PATCH, no direct writes."""
        client = _client(get_rows=[_submission_row()],
            post_response=_ok_issue_result())
        _run_with_client(client,
            request_review_reference(SUBMISSION_ID, REQUEST_KEY))
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()

    def test_issue_bad_arguments_rejected_before_network(self):
        client = _client(post_response=_ok_issue_result())
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            for submission_id, key in (
                    ("not-a-uuid", REQUEST_KEY),
                    (SUBMISSION_ID, ""),
                    (SUBMISSION_ID, "has space"),
                    (SUBMISSION_ID, "x" * 129)):
                with self.assertRaises(WorkflowError,
                        msg="%r/%r" % (submission_id, key)):
                    asyncio.run(request_review_reference(submission_id, key))
        client.post.assert_not_called()

    def test_issue_malformed_reference_rejected(self):
        for body in ({"status": "open", "review_ref": SUBMISSION_ID,
                "submission_id": SUBMISSION_ID, "submission_kind": "sale",
                "request_key": REQUEST_KEY, "is_retry": False},
                {"status": "open", "review_ref": "YR-SHORT",
                "submission_id": SUBMISSION_ID, "submission_kind": "sale",
                "request_key": REQUEST_KEY, "is_retry": False},
                {"status": "open", "review_ref": "YR-0000000000",
                "submission_id": SUBMISSION_ID, "submission_kind": "sale",
                "request_key": REQUEST_KEY, "is_retry": False},
                {"status": "open", "review_ref": REVIEW_REF,
                "submission_id": SUBMISSION_ID, "submission_kind": "sale",
                "request_key": "other-key", "is_retry": False},
                ["open"]):
            client = _client(
                post_response=httpx.Response(200, json=body))
            with self.assertRaises(WorkflowDatabaseError, msg=str(body)):
                _run_with_client(client,
                    request_review_reference(SUBMISSION_ID, REQUEST_KEY))

    def test_issue_idempotent_retry_accepted(self):
        client = _client(post_response=_ok_issue_result(status="open"))
        result, _client_used = _run_with_client(client,
            request_review_reference(SUBMISSION_ID, REQUEST_KEY))
        self.assertEqual(result["review_ref"], REVIEW_REF)

    def test_issue_on_terminal_maps_to_not_reviewable(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "NOT_REVIEWABLE: submission x has status confirmed"}))
        with self.assertRaises(NotReviewableError):
            _run_with_client(client,
                request_review_reference(SUBMISSION_ID, REQUEST_KEY))

    def test_issue_key_conflict_maps_to_typed_error(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "CONFLICT: request key was already used for a different submission"}))
        with self.assertRaises(RequestConflictError):
            _run_with_client(client,
                request_review_reference(SUBMISSION_ID, REQUEST_KEY))


def _confirm(verified, request_key=REQUEST_KEY, review_ref=REVIEW_REF,
        post_response=None, reason=None, sender="+2348000000001"):
    client = _client(post_response=post_response or _ok_confirm_result(
        verified=verified, request_key=request_key, review_ref=review_ref))
    return _run_with_client(client, confirm_submission(review_ref,
        "whatsapp", sender, verified, request_key,
        correction_reason=reason))


class ConfirmRpcTests(unittest.TestCase):
    def test_confirmation_calls_only_the_fixed_confirm_rpc(self):
        result, client = _confirm(_sale_verified())
        self.assertEqual(result["status"], "confirmed")
        client.post.assert_called_once()
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_confirm_submission")

    def test_confirm_payload_carries_no_scope_or_routing(self):
        """No tenant/business/branch, no submission UUID, no provider
        account or destination: scope and routing come from the database."""
        _result, client = _confirm(_sale_verified())
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(set(body),
            {"p_review_ref", "p_reviewer_provider", "p_reviewer_sender",
                "p_verified", "p_request_key", "p_correction_reason"})
        for forbidden in ("tenant_id", "business_id", "branch_id",
                "tenant", "business", "branch", "scope", "p_submission_id",
                "submission_id", "p_provider_account", "provider_account",
                "destination", "recipient"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(body["p_review_ref"], REVIEW_REF)
        self.assertEqual(body["p_request_key"], REQUEST_KEY)

    def test_confirm_performs_no_submission_read(self):
        """The reference is opaque: confirmation is exactly one RPC call
        with no GET and no PATCH."""
        _result, client = _confirm(_sale_verified())
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()

    def test_each_kind_confirms_through_the_same_rpc(self):
        cases = [("production", _production_verified(), "production"),
            ("poultry_daily_report", _production_verified(), "production"),
            ("sale", _sale_verified(), "sale"),
            ("payment", _payment_verified(), "payment"),
            ("expense", _expense_verified(), "expense")]
        for kind, verified, posting_type in cases:
            # The snapshot declares its kind so local pre-validation can
            # check it; the RPC remains authoritative for the real mapping.
            hinted = dict(verified, kind=kind if kind != "poultry_daily_report"
                else "poultry_daily_report")
            client = _client(post_response=_ok_confirm_result(
                posting_type, verified=hinted))
            result, client = _run_with_client(client, confirm_submission(
                REVIEW_REF, "whatsapp", "+2348000000001", hinted,
                REQUEST_KEY))
            self.assertEqual(result["posting_type"], posting_type,
                msg=kind)
            self.assertEqual(client.post.call_args.args[0],
                "/rest/v1/rpc/amose_confirm_submission", msg=kind)
            client.get.assert_not_called()

    def test_raw_uuid_never_accepted_as_review_reference(self):
        """Internal submission UUIDs must never travel over the review
        path: a UUID in the reference slot is rejected before any network
        call, with no database round trip at all."""
        client = _client(post_response=_ok_confirm_result())
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            for bad_ref in (SUBMISSION_ID,
                    "11111111-1111-1111-1111-111111111111",
                    "REVIEW CONFIRM %s KEY k1" % SUBMISSION_ID,
                    "", None, 123,
                    "YR-SHORT", "YR-0000000000", "YR-oooooooooo",
                    "YR-ABCDEFGHIJK", "XR-ABCD234EFG"):
                with self.assertRaises(WorkflowError, msg=str(bad_ref)):
                    asyncio.run(confirm_submission(bad_ref, "whatsapp",
                        "+2348000000001", _sale_verified(), REQUEST_KEY))
                with self.assertRaises(WorkflowError, msg=str(bad_ref)):
                    asyncio.run(reject_submission(bad_ref, "whatsapp",
                        "+2348000000001", "reason", REQUEST_KEY))
        client.post.assert_not_called()
        client.get.assert_not_called()

    def test_bad_arguments_rejected_before_network(self):
        client = _client(post_response=_ok_confirm_result())
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            for kwargs in ({"request_key": ""},
                    {"reviewer_provider": "sms"},
                    {"reviewer_sender": "  "},
                    {"review_ref": "not-a-ref"}):
                base = {"review_ref": REVIEW_REF,
                    "reviewer_provider": "whatsapp",
                    "reviewer_sender": "+2348000000001",
                    "verified": _sale_verified(), "request_key": REQUEST_KEY}
                base.update(kwargs)
                with self.assertRaises(WorkflowError, msg=str(kwargs)):
                    asyncio.run(confirm_submission(**base))
        client.post.assert_not_called()
        client.get.assert_not_called()

    def test_kind_hinted_snapshot_validated_locally(self):
        """A snapshot declaring a supported kind gets strict local
        pre-validation: garbage is rejected before any network call."""
        client = _client(post_response=_ok_confirm_result())
        bad = _sale_verified(kind="sale", lines=[])
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(MalformedVerifiedError):
                asyncio.run(confirm_submission(REVIEW_REF, "whatsapp",
                    "+2348000000001", bad, REQUEST_KEY))
        client.post.assert_not_called()

    def test_unhinted_snapshot_defers_to_rpc_authoritatively(self):
        """Without a kind label the RPC validates: its MALFORMED error maps
        to a typed error and nothing is posted."""
        client = _client(post_response=httpx.Response(400, json={
            "message": "MALFORMED: verified block requires product_id"}))
        with self.assertRaises(MalformedVerifiedError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", {"unexpected": "shape"},
                REQUEST_KEY))
        client.post.assert_called_once()

    def test_correction_reason_recorded_as_corrected(self):
        verified = _sale_verified()
        body = {"status": "confirmed", "review_action": "corrected",
            "posting_type": "sale", "submission_kind": "sale",
            "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
            "request_key": REQUEST_KEY, "verified_snapshot": verified,
            "audit_id": "audit-2", "is_retry": False,
            "sale_id": "sale-9", "brain_memory_id": "mem-9"}
        result, _client_used = _confirm(verified,
            post_response=httpx.Response(200, json=body),
            reason="price fixed from crate label")
        self.assertEqual(result["review_action"], "corrected")

    def test_blank_correction_reason_rejected_locally(self):
        client = _client(post_response=_ok_confirm_result())
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(WorkflowError):
                asyncio.run(confirm_submission(REVIEW_REF, "whatsapp",
                    "+2348000000001", _sale_verified(), REQUEST_KEY,
                    correction_reason="   "))
        client.post.assert_not_called()


class ConfirmResponseTests(unittest.TestCase):
    def _confirm_with_body(self, body, verified=None, status=200):
        return _confirm(verified if verified is not None else _sale_verified(),
            post_response=httpx.Response(status, json=body))

    def _base_body(self, **overrides):
        body = {"status": "confirmed", "review_action": "confirmed",
            "posting_type": "sale", "submission_kind": "sale",
            "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
            "request_key": REQUEST_KEY,
            "verified_snapshot": _sale_verified(),
            "audit_id": "a", "is_retry": False,
            "sale_id": "s", "brain_memory_id": "m"}
        body.update(overrides)
        return body

    def test_kind_posting_mismatch_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(self._base_body(posting_type="payment"))

    def test_unmapped_kind_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(
                self._base_body(submission_kind="whatsapp_message"))

    def test_missing_required_id_rejected(self):
        body = self._base_body()
        del body["sale_id"]
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(body)

    def test_empty_required_id_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(self._base_body(sale_id=""))

    def test_unexpected_status_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(self._base_body(status="posted"))

    def test_malformed_submission_id_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(
                self._base_body(submission_id="not-a-uuid"))

    def test_review_ref_mismatch_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(
                self._base_body(review_ref=REVIEW_REF_2))

    def test_request_key_mismatch_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(
                self._base_body(request_key="other-key"))

    def test_verified_echo_mismatch_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(
                self._base_body(verified_snapshot={"tampered": True}))

    def test_missing_audit_id_rejected(self):
        body = self._base_body()
        del body["audit_id"]
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(body)

    def test_non_object_result_rejected(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_body(["confirmed"])


class ConfirmErrorMappingTests(unittest.TestCase):
    def _confirm_with_rpc_error(self, message, status=400):
        client = _client(
            post_response=httpx.Response(status, json={"message": message}))
        return _run_with_client(client, confirm_submission(REVIEW_REF,
            "whatsapp", "+2348000000001", _sale_verified(), REQUEST_KEY))

    def test_unauthorized_reviewer_rejected(self):
        with self.assertRaises(ReviewUnauthorizedError):
            self._confirm_with_rpc_error(
                "UNAUTHORIZED: reviewer could not be authorized")

    def test_unknown_reference_maps_to_reference_error(self):
        """Cross-tenant guesses read exactly like nonexistent references:
        both arrive as NOT_FOUND about the review reference."""
        with self.assertRaises(ReviewReferenceError):
            self._confirm_with_rpc_error(
                "NOT_FOUND: review reference is not known")

    def test_missing_submission_stays_not_found(self):
        with self.assertRaises(SubmissionNotFoundError):
            self._confirm_with_rpc_error(
                "NOT_FOUND: submission x does not exist")

    def test_unroutable_acknowledgement_maps_to_typed_error(self):
        with self.assertRaises(AcknowledgementRoutingError):
            self._confirm_with_rpc_error(
                "ROUTING: original report sender has no verified sender identity")

    def test_not_reviewable_maps_to_typed_error(self):
        with self.assertRaises(NotReviewableError):
            self._confirm_with_rpc_error(
                "NOT_REVIEWABLE: submission x has status confirmed")

    def test_changed_payload_reuse_rejected(self):
        with self.assertRaises(RequestConflictError):
            self._confirm_with_rpc_error(
                "CONFLICT: request key was already used with different verified data")

    def test_malformed_maps_to_typed_error(self):
        with self.assertRaises(MalformedVerifiedError):
            self._confirm_with_rpc_error(
                "MALFORMED: submission x has no verified sale block")

    def test_scope_maps_to_typed_error(self):
        with self.assertRaises(ScopeMismatchError):
            self._confirm_with_rpc_error(
                "SCOPE: product x is not an active product")

    def test_approval_maps_to_typed_error(self):
        with self.assertRaises(ApprovalRequiredError):
            self._confirm_with_rpc_error(
                "APPROVAL: approval request x is not approved")

    def test_incomplete_maps_to_typed_error(self):
        with self.assertRaises(IncompletePostingError):
            self._confirm_with_rpc_error(
                "INCOMPLETE: submission x sale posting is missing its brain memory")

    def test_unknown_failure_is_workflow_database_error(self):
        with self.assertRaises(WorkflowDatabaseError):
            self._confirm_with_rpc_error("something unexpected", status=500)

    def test_retry_returns_the_original_result(self):
        verified = _sale_verified()
        body = {"status": "confirmed", "review_action": "confirmed",
            "posting_type": "sale", "submission_kind": "sale",
            "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
            "request_key": REQUEST_KEY, "verified_snapshot": verified,
            "audit_id": "audit-1", "is_retry": True,
            "sale_id": "sale-1", "brain_memory_id": "mem-1"}
        result, client = _confirm(verified,
            post_response=httpx.Response(200, json=body))
        self.assertEqual(result, body)
        self.assertTrue(result["is_retry"])
        client.post.assert_called_once()


class SelfReviewTests(unittest.TestCase):
    """Adversarial separation-of-duties tests: the reporter must never
    confirm or reject their own report. The database refuses with the
    generic UNAUTHORIZED message (indistinguishable from any other auth
    failure); these tests prove the refusal surfaces as a typed
    ReviewUnauthorizedError after exactly one RPC call, with no submission
    read, no PATCH, and no second RPC attempt -- so no status change, audit
    row, operational posting, Brain memory, or outbound acknowledgement can
    result. (Row-level proof with status/audit/outbound counts is covered
    by the transactional SQL probes, which roll back.)"""

    def _self_review_error(self, message="UNAUTHORIZED: reviewer could not be authorized"):
        return httpx.Response(400, json={"message": message})

    def test_self_confirmation_blocked(self):
        client = _client(post_response=self._self_review_error())
        with self.assertRaises(ReviewUnauthorizedError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", _sale_verified(), REQUEST_KEY))
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()

    def test_self_rejection_blocked(self):
        client = _client(post_response=self._self_review_error())
        with self.assertRaises(ReviewUnauthorizedError):
            _run_with_client(client, reject_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", "fixing my own report",
                REQUEST_KEY))
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()

    def test_self_review_error_reveals_nothing(self):
        """The refusal message is the generic authorization failure: it
        names no employee, role, scope, or submission detail."""
        client = _client(post_response=self._self_review_error())
        try:
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", _sale_verified(), REQUEST_KEY))
            self.fail("expected ReviewUnauthorizedError")
        except ReviewUnauthorizedError as error:
            message = str(error).lower()
            for leaked in ("employee", "reporter", "self", "own",
                    SUBMISSION_ID.lower()):
                self.assertNotIn(leaked, message)


class TenantScopedIdempotencyTests(unittest.TestCase):
    """Idempotency keys are scoped to the tenant by the database
    (UNIQUE (tenant_id, request_key)). At this boundary that means: the
    payload carries NO tenant field (scope resolves from the reference
    inside the reviewer's own tenant), the SAME key succeeds under two
    different references, and CHANGED reuse under one reference still fails
    closed. (Cross-tenant key reuse with row counts is covered by the
    transactional SQL probes, which roll back.)"""

    def test_same_key_succeeds_under_two_references(self):
        """Two tenants may use the same request key: each reference
        resolves and confirms independently."""
        for ref in (REVIEW_REF, REVIEW_REF_2):
            verified = _sale_verified()
            client = _client(post_response=httpx.Response(200, json={
                "status": "confirmed", "review_action": "confirmed",
                "posting_type": "sale", "submission_kind": "sale",
                "submission_id": SUBMISSION_ID, "review_ref": ref,
                "request_key": REQUEST_KEY, "verified_snapshot": verified,
                "audit_id": "audit-" + ref, "is_retry": False,
                "sale_id": "sale-" + ref, "brain_memory_id": "mem-" + ref}))
            result, client = _run_with_client(client, confirm_submission(
                ref, "whatsapp", "+2348000000001", verified, REQUEST_KEY))
            self.assertEqual(result["review_ref"], ref)
            self.assertEqual(result["request_key"], REQUEST_KEY)
            body = client.post.call_args.kwargs["json"]
            for forbidden in ("tenant_id", "business_id", "branch_id"):
                self.assertNotIn(forbidden, body)

    def test_changed_reuse_inside_one_reference_still_fails(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "CONFLICT: request key was already used with different verified data"}))
        with self.assertRaises(RequestConflictError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001",
                _sale_verified(discount_kobo=1000), REQUEST_KEY))
        client.post.assert_called_once()

    def test_reject_changed_reuse_still_fails(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "CONFLICT: request key was already used with a different reason"}))
        with self.assertRaises(RequestConflictError):
            _run_with_client(client, reject_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", "other reason", REQUEST_KEY))


class ApprovedExpenseTests(unittest.TestCase):
    """Adversarial approved-expense path: a confirmed expense referencing a
    same-tenant approved approval request posts expense + Brain memory;
    pending, rejected, or cross-tenant approvals are refused with
    ApprovalRequiredError after exactly one RPC call (no confirmation,
    audit, expense, custody, Brain, or outbound row can result -- the RPC
    transaction rolls back; row-level proof is in the SQL probes)."""

    def _confirm_expense(self, verified, post_response):
        client = _client(post_response=post_response)
        return _run_with_client(client, confirm_submission(REVIEW_REF,
            "whatsapp", "+2348000000001", verified, REQUEST_KEY))

    def test_approved_expense_confirms_end_to_end(self):
        verified = _expense_verified(kind="expense",
            approval_request_id=APPROVAL_ID)
        body = {"status": "confirmed", "review_action": "confirmed",
            "posting_type": "expense", "submission_kind": "expense",
            "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
            "request_key": REQUEST_KEY, "verified_snapshot": verified,
            "audit_id": "audit-exp", "is_retry": False,
            "expense_id": "exp-1", "brain_memory_id": "mem-exp"}
        result, client = self._confirm_expense(verified,
            httpx.Response(200, json=body))
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["expense_id"], "exp-1")
        self.assertEqual(result["brain_memory_id"], "mem-exp")
        sent = client.post.call_args.kwargs["json"]
        self.assertEqual(sent["p_verified"]["approval_request_id"],
            APPROVAL_ID)
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()

    def test_pending_approval_blocked(self):
        verified = _expense_verified(kind="expense",
            approval_request_id=APPROVAL_ID)
        client = _client(post_response=httpx.Response(400, json={
            "message": "APPROVAL: approval request %s is pending for this tenant"
            % APPROVAL_ID}))
        with self.assertRaises(ApprovalRequiredError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", verified, REQUEST_KEY))
        client.post.assert_called_once()
        client.patch.assert_not_called()

    def test_rejected_approval_blocked(self):
        verified = _expense_verified(kind="expense",
            approval_request_id=APPROVAL_ID)
        client = _client(post_response=httpx.Response(400, json={
            "message": "APPROVAL: approval request %s is not approved for this tenant"
            % APPROVAL_ID}))
        with self.assertRaises(ApprovalRequiredError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", verified, REQUEST_KEY))
        client.post.assert_called_once()

    def test_cross_tenant_approval_blocked(self):
        """An approval from another tenant reads as 'not approved for this
        tenant': no cross-tenant pointer is ever followed."""
        verified = _expense_verified(kind="expense",
            approval_request_id=APPROVAL_ID)
        client = _client(post_response=httpx.Response(400, json={
            "message": "APPROVAL: approval request %s is not approved for this tenant"
            % APPROVAL_ID}))
        with self.assertRaises(ApprovalRequiredError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", verified, REQUEST_KEY))
        client.post.assert_called_once()
        client.patch.assert_not_called()

    def test_failed_approval_makes_no_other_call(self):
        """A refused expense performs exactly one RPC call and nothing
        else: no reads, no PATCH writes, no second RPC attempt."""
        verified = _expense_verified(kind="expense",
            approval_request_id=APPROVAL_ID)
        client = _client(post_response=httpx.Response(400, json={
            "message": "APPROVAL: approval request x is not approved"}))
        with self.assertRaises(ApprovalRequiredError):
            _run_with_client(client, confirm_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", verified, REQUEST_KEY))
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()


class RejectRpcTests(unittest.TestCase):
    def _reject(self, reason="duplicate report", request_key=REQUEST_KEY,
            review_ref=REVIEW_REF, post_response=None):
        client = _client(post_response=post_response or _ok_reject_result(
            request_key=request_key, review_ref=review_ref, reason=reason))
        return _run_with_client(client, reject_submission(review_ref,
            "whatsapp", "+2348000000001", reason, request_key))

    def test_rejection_calls_only_the_fixed_reject_rpc(self):
        result, client = self._reject()
        self.assertEqual(result["status"], "rejected")
        client.post.assert_called_once()
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_reject_submission")

    def test_reject_payload_carries_no_scope_verified_or_routing(self):
        _result, client = self._reject()
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(set(body),
            {"p_review_ref", "p_reviewer_provider", "p_reviewer_sender",
                "p_reason", "p_request_key"})
        for forbidden in ("tenant_id", "business_id", "branch_id",
                "p_verified", "verified", "p_submission_id", "submission_id",
                "p_provider_account", "provider_account"):
            self.assertNotIn(forbidden, body)

    def test_blank_reason_rejected_before_network(self):
        client = _client(post_response=_ok_reject_result())
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            for bad in ("", "   ", None, 123):
                with self.assertRaises(WorkflowError, msg=str(bad)):
                    asyncio.run(reject_submission(REVIEW_REF, "whatsapp",
                        "+2348000000001", bad, REQUEST_KEY))
        client.post.assert_not_called()

    def test_reject_needs_no_submission_read(self):
        """Rejection carries no kind-dependent validation, so it performs
        no GET -- exactly one RPC call and nothing else."""
        _result, client = self._reject()
        client.get.assert_not_called()
        client.post.assert_called_once()

    def test_reject_retry_returns_original(self):
        body = {"status": "rejected", "review_action": "rejected",
            "submission_kind": "sale", "submission_id": SUBMISSION_ID,
            "review_ref": REVIEW_REF,
            "request_key": REQUEST_KEY, "reason": "duplicate report",
            "audit_id": "audit-9", "is_retry": True}
        result, _client_used = self._reject(
            post_response=httpx.Response(200, json=body))
        self.assertEqual(result, body)
        self.assertTrue(result["is_retry"])

    def test_reject_conflict_maps_to_typed_error(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "CONFLICT: request key was already used with a different reason"}))
        with self.assertRaises(RequestConflictError):
            _run_with_client(client, reject_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", "other reason", REQUEST_KEY))

    def test_reject_on_confirmed_maps_to_not_reviewable(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "NOT_REVIEWABLE: submission x has status confirmed"}))
        with self.assertRaises(NotReviewableError):
            _run_with_client(client, reject_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", "too late", REQUEST_KEY))

    def test_reject_unknown_reference_maps_to_reference_error(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "NOT_FOUND: review reference is not known"}))
        with self.assertRaises(ReviewReferenceError):
            _run_with_client(client, reject_submission(REVIEW_REF,
                "whatsapp", "+2348000000001", "reason", REQUEST_KEY))

    def test_reject_malformed_result_rejected(self):
        for body in (["rejected"],
                {"status": "rejected", "review_action": "confirmed",
                    "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
                    "request_key": REQUEST_KEY, "reason": "reason",
                    "audit_id": "a"},
                {"status": "rejected", "review_action": "rejected",
                    "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
                    "request_key": "other", "reason": "reason",
                    "audit_id": "a"},
                {"status": "rejected", "review_action": "rejected",
                    "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF_2,
                    "request_key": REQUEST_KEY, "reason": "reason",
                    "audit_id": "a"},
                {"status": "rejected", "review_action": "rejected",
                    "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
                    "request_key": REQUEST_KEY, "reason": "changed",
                    "audit_id": "a"}):
            client = _client(
                post_response=httpx.Response(200, json=body))
            with self.assertRaises(WorkflowDatabaseError, msg=str(body)):
                _run_with_client(client, reject_submission(REVIEW_REF,
                    "whatsapp", "+2348000000001", "reason", REQUEST_KEY))


class CrossTenantReferenceTests(unittest.TestCase):
    """A reference guess from another tenant must fail exactly like a
    nonexistent reference: ReviewReferenceError, one RPC call, nothing
    else. (Row-level proof that no cross-tenant row is touched is in the
    SQL probes, which roll back.)"""

    def test_cross_tenant_guess_is_reference_error(self):
        client = _client(post_response=httpx.Response(400, json={
            "message": "NOT_FOUND: review reference is not known"}))
        with self.assertRaises(ReviewReferenceError):
            _run_with_client(client, confirm_submission(REVIEW_REF_2,
                "whatsapp", "+2348000000001", _sale_verified(), REQUEST_KEY))
        client.get.assert_not_called()
        client.patch.assert_not_called()
        client.post.assert_called_once()

    def test_reference_error_is_workflow_error_but_not_not_found(self):
        self.assertTrue(issubclass(ReviewReferenceError, WorkflowError))
        self.assertFalse(issubclass(ReviewReferenceError,
            SubmissionNotFoundError))
        self.assertFalse(issubclass(SubmissionNotFoundError,
            ReviewReferenceError))


class WorkflowHierarchyTests(unittest.TestCase):
    def test_database_error_in_both_hierarchies(self):
        self.assertTrue(issubclass(WorkflowDatabaseError, WorkflowError))
        self.assertTrue(issubclass(WorkflowDatabaseError, DatabaseUnavailable))

    def test_routing_error_fails_closed_in_both_hierarchies(self):
        self.assertTrue(issubclass(AcknowledgementRoutingError, WorkflowError))
        self.assertTrue(issubclass(AcknowledgementRoutingError,
            WorkflowDatabaseError))
        self.assertTrue(issubclass(AcknowledgementRoutingError,
            DatabaseUnavailable))

    def test_all_domain_errors_are_workflow_errors(self):
        for error in (ReviewValidationError, ReviewReferenceError,
                UnsupportedKindError,
                NotReviewableError, ReviewUnauthorizedError,
                MalformedVerifiedError, ScopeMismatchError,
                ApprovalRequiredError, SubmissionNotFoundError,
                IncompletePostingError, RequestConflictError,
                AcknowledgementRoutingError):
            self.assertTrue(issubclass(error, WorkflowError), msg=error)

    def test_missing_configuration_is_a_workflow_error(self):
        with patch("human_confirmation.credentials",
                   side_effect=DatabaseUnavailable("missing")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            with self.assertRaises(WorkflowError):
                asyncio.run(preview_submission(SUBMISSION_ID,
                    _sale_verified()))
            factory.assert_not_called()

    def test_http_failure_is_a_workflow_error(self):
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(WorkflowError):
                asyncio.run(confirm_submission(REVIEW_REF, "whatsapp",
                    "+2348000000001", _sale_verified(), REQUEST_KEY))

    def test_no_secret_leakage_in_errors(self):
        try:
            human_confirmation._require_sender("whatsapp", "   ")
        except ReviewValidationError as error:
            self.assertNotIn("test-key", str(error))
            self.assertNotIn("eyJ", str(error))


class ReviewCommandParserTests(unittest.TestCase):
    def _command(self, action, ref=REVIEW_REF, key=REQUEST_KEY, reason=None):
        base = "REVIEW %s %s KEY %s" % (action, ref, key)
        if action == "CONFIRM" and reason:
            return base + " CORRECTION " + reason
        if action == "REJECT":
            return base + " REASON " + (reason or "duplicate report")
        return base

    def test_casual_words_never_approve(self):
        for text in ("yes", "Yes", "YES", "ok", "OK", "confirm", "Confirm",
                "confirmed", "approve", "approved", "looks good", "go ahead",
                "yes confirm", "ok thanks", "CONFIRM", "REJECT",
                "REVIEW", "REVIEW CONFIRM", "YR-ABCD234EFG"):
            self.assertIsNone(parse_review_command(text), msg=text)

    def test_ordinary_reports_never_parse(self):
        for text in ("Sold 50 bags at 500 NGN", "4 bags feed today",
                "Spent 75000 on diesel",
                "REVIEW my report please",
                "CONFIRM %s" % REVIEW_REF,
                "REVIEW CONFIRM %s" % REVIEW_REF):
            self.assertIsNone(parse_review_command(text), msg=text)

    def test_raw_uuids_never_parse(self):
        """Internal submission UUIDs must never be required -- or accepted
        -- in WhatsApp commands, in either slot or position."""
        uuid_commands = [
            "REVIEW CONFIRM %s KEY %s" % (SUBMISSION_ID, REQUEST_KEY),
            "REVIEW REJECT %s KEY %s REASON duplicate"
            % (SUBMISSION_ID, REQUEST_KEY),
            "REVIEW CONFIRM %s KEY %s CORRECTION fix"
            % (SUBMISSION_ID, REQUEST_KEY),
            "REVIEW %s CONFIRM KEY k1" % SUBMISSION_ID,
        ]
        for text in uuid_commands:
            self.assertIsNone(parse_review_command(text), msg=text)

    def test_ambiguous_alphabet_never_parses(self):
        """0/O and 1/I/L are not in the alphabet: lookalike references are
        rejected rather than guessed."""
        for ref in ("YR-0000000000", "YR-1111111111", "YR-OOOOOOOOOO",
                "YR-IIIIIIIIII", "YR-LLLLLLLLLL", "YR-ABCD234EFO",
                "yr-abcd234ef1"):
            self.assertIsNone(
                parse_review_command("REVIEW CONFIRM %s KEY k1" % ref),
                msg=ref)

    def test_explicit_confirm_parses_unambiguously(self):
        parsed = parse_review_command(self._command("CONFIRM"))
        self.assertEqual(parsed, {"action": "confirm",
            "review_ref": REVIEW_REF, "request_key": REQUEST_KEY,
            "reason": None})
        self.assertNotIn("submission_id", parsed)

    def test_explicit_confirm_with_correction_parses(self):
        parsed = parse_review_command(
            self._command("CONFIRM", reason="price fixed from crate label"))
        self.assertEqual(parsed["action"], "confirm")
        self.assertEqual(parsed["review_ref"], REVIEW_REF)
        self.assertEqual(parsed["reason"], "price fixed from crate label")

    def test_explicit_reject_parses(self):
        parsed = parse_review_command(self._command("REJECT"))
        self.assertEqual(parsed, {"action": "reject",
            "review_ref": REVIEW_REF, "request_key": REQUEST_KEY,
            "reason": "duplicate report"})

    def test_lowercase_reference_normalized(self):
        parsed = parse_review_command(
            "review confirm %s key %s" % (REVIEW_REF.lower(), REQUEST_KEY))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["review_ref"], REVIEW_REF)

    def test_reject_without_reason_never_parses(self):
        self.assertIsNone(parse_review_command(
            "REVIEW REJECT %s KEY %s" % (REVIEW_REF, REQUEST_KEY)))

    def test_malformed_commands_never_parse(self):
        cases = ["REVIEW CONFIRM not-a-ref KEY k1",
            "REVIEW CONFIRM %s" % REVIEW_REF,
            "REVIEW CONFIRM %s KEY " % REVIEW_REF,
            "REVIEW CONFIRM %s KEY k 1" % REVIEW_REF,
            "REVIEW REJECT %s KEY k1 REASON " % REVIEW_REF,
            "CONFIRM %s KEY k1" % REVIEW_REF,
            "REVIEW MAYBE %s KEY k1" % REVIEW_REF,
            "",
            None,
            123]
        for text in cases:
            self.assertIsNone(parse_review_command(text), msg=str(text))

    def test_keywords_are_case_insensitive_but_order_is_fixed(self):
        parsed = parse_review_command(
            "review confirm %s key %s" % (REVIEW_REF, REQUEST_KEY))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["action"], "confirm")
        self.assertIsNone(parse_review_command(
            "REVIEW KEY k1 CONFIRM %s" % REVIEW_REF))

    def test_unknown_sender_still_needs_identity(self):
        """Parsing is only syntax: sender identity and authorization are
        enforced inside the database RPC, never by the parser. An unknown
        sender therefore fails closed at confirm time."""
        parsed = parse_review_command(self._command("CONFIRM"))
        self.assertIsNotNone(parsed)
        client = _client(
            post_response=httpx.Response(400, json={
                "message": "UNAUTHORIZED: reviewer could not be authorized"}))
        with self.assertRaises(ReviewUnauthorizedError):
            _run_with_client(client, confirm_submission(
                parsed["review_ref"], "whatsapp", "+2340000000000",
                _sale_verified(), parsed["request_key"]))


class ExecuteCommandTests(unittest.TestCase):
    def test_confirm_command_dispatches_by_reference(self):
        verified = _sale_verified()
        command = parse_review_command(
            "REVIEW CONFIRM %s KEY %s" % (REVIEW_REF, REQUEST_KEY))
        client = _client(post_response=_ok_confirm_result(verified=verified))
        result, client = _run_with_client(client, execute_review_command(
            command, "whatsapp", "+2348000000001", verified=verified))
        self.assertEqual(result["status"], "confirmed")
        body = client.post.call_args.kwargs["json"]
        # Reviewer identity comes from the sender arguments, never from
        # the parsed text (which carries no identity at all).
        self.assertEqual(body["p_reviewer_sender"], "+2348000000001")
        self.assertEqual(body["p_review_ref"], REVIEW_REF)
        self.assertNotIn("submission_id", command)
        client.get.assert_not_called()

    def test_reject_command_dispatches_with_inline_reason(self):
        command = parse_review_command(
            "REVIEW REJECT %s KEY %s REASON duplicate report"
            % (REVIEW_REF, REQUEST_KEY))
        client = _client(post_response=_ok_reject_result())
        result, client = _run_with_client(client, execute_review_command(
            command, "whatsapp", "+2348000000001"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_reject_submission")

    def test_confirm_command_without_verified_snapshot_refused(self):
        command = parse_review_command(
            "REVIEW CONFIRM %s KEY %s" % (REVIEW_REF, REQUEST_KEY))
        client = _client(post_response=_ok_confirm_result())
        with patch("human_confirmation.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("human_confirmation.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(MalformedVerifiedError):
                asyncio.run(execute_review_command(
                    command, "whatsapp", "+2348000000001"))
        client.post.assert_not_called()

    def test_unknown_action_refused(self):
        client = _client(post_response=_ok_confirm_result())
        with self.assertRaises(ReviewValidationError):
            asyncio.run(execute_review_command(
                {"action": "maybe", "review_ref": REVIEW_REF,
                    "request_key": REQUEST_KEY},
                "whatsapp", "+2348000000001", verified=_sale_verified()))
        client.post.assert_not_called()

    def test_unparsed_command_refused(self):
        for bad in (None, "REVIEW CONFIRM x KEY y", 123):
            with self.assertRaises(ReviewValidationError):
                asyncio.run(execute_review_command(bad, "whatsapp",
                    "+2348000000001", verified=_sale_verified()))


class NoIngestionConfirmationTests(unittest.TestCase):
    def test_ordinary_message_ingestion_never_confirms_or_posts(self):
        """Ordinary inbox processing (receipt -> draft submission) makes no
        human-confirmation call, even for a fully-parsed sale message."""
        import message_processor
        inbox_rows = [{"id": "inbox-1", "provider": "whatsapp",
            "provider_account": "acct-1",
            "payload": {"kind": "message", "event": {
                "from": "+12025550123", "type": "text",
                "text": {"body": "Sold 50 bags at 500 NGN"}}}}]
        client = AsyncMock()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_message_inbox":
                return httpx.Response(200, json=inbox_rows)
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[{"tenant_id": "t-1",
                    "employee_id": "emp-1"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=[{"business_id": "b-1",
                    "branch_id": "br-1"}])
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": "sub-1"}])
            raise AssertionError("unexpected GET " + path)

        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(return_value=httpx.Response(201))
        client.patch = AsyncMock(return_value=httpx.Response(204))
        with patch("message_processor.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("message_processor.httpx.AsyncClient") as factory, \
             patch("human_confirmation.confirm_submission",
                   new_callable=AsyncMock) as confirm, \
             patch("human_confirmation.reject_submission",
                   new_callable=AsyncMock) as reject, \
             patch("human_confirmation.preview_submission",
                   new_callable=AsyncMock) as preview, \
             patch("human_confirmation.request_review_reference",
                   new_callable=AsyncMock) as issue, \
             patch("human_confirmation.execute_review_command",
                   new_callable=AsyncMock) as execute:
            factory.return_value.__aenter__.return_value = client
            summary = asyncio.run(message_processor.process_inbox_batch())
        self.assertEqual(summary["submitted"], 1)
        confirm.assert_not_called()
        reject.assert_not_called()
        preview.assert_not_called()
        issue.assert_not_called()
        execute.assert_not_called()
        # Belt and braces: ingestion must not even import the workflow.
        self.assertNotIn("human_confirmation", dir(message_processor))
        import whatsapp_webhook
        self.assertNotIn("human_confirmation", dir(whatsapp_webhook))
        self.assertNotIn("execute_review_command", dir(message_processor))
        self.assertNotIn("execute_review_command", dir(whatsapp_webhook))


if __name__ == "__main__":
    unittest.main()
