"""Stage 1 tests: complete daily business records for YAMSI BizLite.

Unit tests use the same mocking conventions as the rest of the suite
(patch httpx.AsyncClient with an AsyncMock client; real httpx.Response
objects for payloads). No test touches a live database and no test invents
business IDs beyond local throwaway fixtures.
"""
import asyncio
import re
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import human_confirmation
import message_processor
import operational_posting
import operator_pay
import review_service
import rule_engine
import water_intake
from human_confirmation import (
    MalformedVerifiedError,
    RequestConflictError,
    ScopeMismatchError,
    UnsupportedKindError,
    posting_for_kind as review_posting_for_kind,
    validate_verified,
)
from operational_posting import (
    PostingDatabaseError,
    UnsupportedKindError as PostingUnsupportedKind,
    post_submission,
    posting_for_kind as operational_posting_for_kind,
)

SUBMISSION_ID = "11111111-1111-1111-1111-111111111111"
FROM_ID = "22222222-2222-2222-2222-222222222222"
TO_ID = "33333333-3333-3333-3333-333333333333"
REVIEW_REF = "YR-ABCD234EFG"


# ---------------------------------------------------------------------------
# 1. Deterministic extraction: valid messages for each kind.
# ---------------------------------------------------------------------------

class ValidExtractionTests(unittest.TestCase):
    def test_production(self):
        result = water_intake.extract_water_record(
            "Produced 250 good bags, 5 rejected")
        self.assertEqual(result["kind"], "production")
        self.assertEqual(result["fields"]["good_quantity"], 250)
        self.assertEqual(result["fields"]["rejected_quantity"], 5)
        self.assertEqual(result["provenance"]["good_quantity"],
            "staff_reported")
        self.assertEqual(result["provenance"]["rejected_quantity"],
            "staff_reported")
        self.assertEqual(result["message_text"],
            "Produced 250 good bags, 5 rejected")

    def test_sale(self):
        result = water_intake.extract_water_record(
            "Sold 100 bags at 500 cash")
        self.assertEqual(result["kind"], "sale")
        self.assertEqual(result["fields"]["quantity"], 100.0)
        self.assertEqual(result["fields"]["unit"], "bag")
        self.assertEqual(result["fields"]["unit_price"], 500.0)
        self.assertEqual(result["fields"]["payment_method"], "cash")
        self.assertEqual(result["errors"], [])

    def test_payment_received(self):
        result = water_intake.extract_water_record(
            "Received 50000 cash for sale YR-ABCD234EFG")
        self.assertEqual(result["kind"], "payment")
        self.assertEqual(result["fields"]["amount_kobo"], 5000000)
        self.assertEqual(result["fields"]["method"], "cash")
        self.assertEqual(result["fields"]["sale_ref"], "YR-ABCD234EFG")
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["errors"], [])

    def test_expense(self):
        result = water_intake.extract_water_record(
            "Spent 20000 on tricycle fuel cash")
        self.assertEqual(result["kind"], "expense")
        self.assertEqual(result["fields"]["amount_kobo"], 2000000)
        self.assertEqual(result["fields"]["category"], "fuel")
        self.assertEqual(result["fields"]["description"], "tricycle fuel")
        self.assertEqual(result["fields"]["payment_method"], "cash")
        self.assertEqual(result["errors"], [])

    def test_cash_handover(self):
        result = water_intake.extract_water_record(
            "Michael handed 80000 cash to Vivian")
        self.assertEqual(result["kind"], "cash_handover")
        self.assertEqual(result["fields"]["amount_kobo"], 8000000)
        self.assertEqual(result["fields"]["from_name"], "Michael")
        self.assertEqual(result["fields"]["to_name"], "Vivian")
        self.assertEqual(result["provenance"]["from_name"], "staff_reported")
        self.assertEqual(result["errors"], [])

    def test_bank_deposit(self):
        result = water_intake.extract_water_record(
            "Vivian deposited 70000 to Access Bank, reference ABC123")
        self.assertEqual(result["kind"], "bank_deposit")
        self.assertEqual(result["fields"]["amount_kobo"], 7000000)
        self.assertEqual(result["fields"]["depositor_name"], "Vivian")
        self.assertEqual(result["fields"]["destination_account"],
            "Access Bank")
        self.assertEqual(result["fields"]["reference"], "ABC123")
        self.assertEqual(result["errors"], [])

    def test_message_variants(self):
        production = water_intake.extract_water_record(
            "Production today: produced 300 bags, 12 damaged")
        self.assertEqual(production["kind"], "production")
        self.assertEqual(production["fields"]["good_quantity"], 300)
        self.assertEqual(production["fields"]["rejected_quantity"], 12)
        handover = water_intake.extract_water_record(
            "Adaeze gave 45000 to Obi")
        self.assertEqual(handover["kind"], "cash_handover")
        self.assertEqual(handover["fields"]["from_name"], "Adaeze")
        self.assertEqual(handover["fields"]["to_name"], "Obi")
        deposit = water_intake.extract_water_record(
            "deposited 90000 to First Bank ref FBX-99")
        self.assertEqual(deposit["kind"], "bank_deposit")
        self.assertEqual(deposit["fields"]["reference"], "FBX-99")


# ---------------------------------------------------------------------------
# 2. Incomplete messages: nothing inferred, missing_fields recorded.
# ---------------------------------------------------------------------------

class IncompleteExtractionTests(unittest.TestCase):
    def test_production_without_rejected(self):
        result = water_intake.extract_water_record("Produced 200 bags")
        self.assertEqual(result["kind"], "production")
        self.assertEqual(result["fields"]["good_quantity"], 200)
        self.assertIn("rejected_quantity", result["missing_fields"])
        self.assertIn("product_id", result["missing_fields"])

    def test_sale_without_price(self):
        result = water_intake.extract_water_record("Sold 100 bags")
        self.assertEqual(result["kind"], "sale")
        self.assertIn("unit_price", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_payment_without_sale_reference(self):
        result = water_intake.extract_water_record("Received 30000 cash")
        self.assertEqual(result["kind"], "payment")
        self.assertEqual(result["fields"]["amount_kobo"], 3000000)
        self.assertIn("sale_ref", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_payment_without_method(self):
        result = water_intake.extract_water_record(
            "Received 30000 for sale YR-ABCD234EFG")
        self.assertIn("method", result["missing_fields"])

    def test_expense_without_category(self):
        result = water_intake.extract_water_record("Spent 20000 cash")
        self.assertEqual(result["kind"], "expense")
        self.assertIn("category", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_handover_without_receiver(self):
        result = water_intake.extract_water_record("Michael handed 80000")
        self.assertEqual(result["kind"], "cash_handover")
        self.assertIn("to_name", result["missing_fields"])

    def test_deposit_without_reference_never_confirms(self):
        result = water_intake.extract_water_record(
            "Vivian deposited 70000 to Access Bank")
        self.assertEqual(result["kind"], "bank_deposit")
        self.assertIn("reference", result["missing_fields"])
        self.assertTrue(any("reference" in error.lower()
            for error in result["errors"]))

    def test_deposit_without_destination(self):
        result = water_intake.extract_water_record(
            "Vivian deposited 70000, reference ABC123")
        self.assertIn("destination_account", result["missing_fields"])

    def test_empty_and_unrecognized(self):
        empty = water_intake.extract_water_record("")
        self.assertIsNone(empty["kind"])
        self.assertEqual(empty["errors"],
            ["Empty or missing message text"])
        unknown = water_intake.extract_water_record(
            "Good morning, please call me")
        self.assertIsNone(unknown["kind"])
        self.assertTrue(unknown["errors"])


# ---------------------------------------------------------------------------
# 3. Ambiguous wording, naira formats, and branch prefixes.
# ---------------------------------------------------------------------------

class AmbiguityAndFormatTests(unittest.TestCase):
    def test_sale_plus_payment_is_ambiguous(self):
        result = water_intake.extract_water_record(
            "Sold 100 bags, received 50000")
        self.assertIsNone(result["kind"])
        self.assertTrue(any("mbiguous" in error
            for error in result["errors"]))

    def test_production_plus_sale_is_ambiguous(self):
        result = water_intake.extract_water_record(
            "Produced 200 bags, sold 50 bags at 500")
        self.assertIsNone(result["kind"])
        self.assertTrue(result["errors"])

    def test_naira_formats(self):
        cases = {
            "Received \u20a650,000 cash for sale YR-ABCD234EFG": 5000000,
            "Received NGN 50000 cash for sale YR-ABCD234EFG": 5000000,
            "Received 50000 naira cash for sale YR-ABCD234EFG": 5000000,
            "Received 50000.50 naira cash for sale YR-ABCD234EFG": 5000050,
            "Spent NGN 20,000 on fuel cash": 2000000,
        }
        for text, expected_kobo in cases.items():
            result = water_intake.extract_water_record(text)
            self.assertEqual(result["fields"].get("amount_kobo"),
                expected_kobo, msg=text)
            self.assertEqual(
                result["provenance"].get("amount_kobo"), "staff_reported",
                msg=text)

    def test_bare_k_suffix_is_ambiguous(self):
        result = water_intake.extract_water_record(
            "Received 50k cash for sale YR-ABCD234EFG")
        self.assertIn("amount_kobo", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_branch_prefix_flows_to_scope(self):
        resolved = message_processor._resolve_multi_assignment(
            [("amose_table_water", "asaba"), ("amose_table_water", "warri")],
            "ASABA: Michael handed 80000 cash to Vivian")
        self.assertEqual(resolved,
            (("amose_table_water", "asaba"),
                "Michael handed 80000 cash to Vivian"))
        extraction = rule_engine.extract(
            "amose_table_water", "asaba", resolved[1])
        self.assertEqual(extraction["kind"], "cash_handover")
        self.assertEqual(extraction["fields"]["to_name"], "Vivian")

    def test_water_production_gets_system_date(self):
        extraction = rule_engine.extract("amose_table_water", "asaba",
            "Produced 250 good bags, 5 rejected",
            received_at="2026-09-20T08:00:00Z")
        self.assertEqual(extraction["kind"], "production")
        self.assertEqual(extraction["fields"]["production_date"],
            "2026-09-20")
        self.assertEqual(extraction["provenance"]["production_date"],
            "system_derived")

    def test_legacy_sale_shape_preserved(self):
        extraction = rule_engine.extract("amose_table_water", "asaba",
            "Sold 50 bags at 500")
        self.assertEqual(extraction["kind"], "sale")
        self.assertEqual(extraction["fields"]["quantity"], 50.0)


# ---------------------------------------------------------------------------
# 4. Expense categories: existing plus task force, ATWAP, tricycle service.
# ---------------------------------------------------------------------------

class ExpenseCategoryTests(unittest.TestCase):
    def test_new_categories(self):
        cases = {
            "Spent 5000 on task force payment cash": "task_force",
            "Spent 15000 on ATWAP dues transfer": "atwap_dues",
            "Spent 8000 on tricycle service cash": "tricycle_service",
            "Spent 20000 on tricycle fuel cash": "fuel",
            "Spent 60000 on operator pay cash": "salaries",
            "Spent 12000 on NEPA bill transfer": "utilities",
            "Spent 3000 on miscellaneous cash": "other",
        }
        for text, category in cases.items():
            result = water_intake.extract_water_record(text)
            self.assertEqual(result["fields"].get("category"), category,
                msg=text)
            self.assertIn("category", result["provenance"], msg=text)

    def test_staff_description_preserved(self):
        result = water_intake.extract_water_record(
            "Spent 5000 on task force levy at gate cash")
        self.assertEqual(result["fields"]["category"], "task_force")
        self.assertIn("task force", result["fields"]["description"])

    def test_new_categories_pass_review_validation(self):
        for category in ("task_force", "atwap_dues", "tricycle_service",
                "fuel", "other"):
            checked = validate_verified("expense", {
                "category": category, "description": "Staff note",
                "amount_kobo": 1000})
            self.assertEqual(checked["posting_type"], "expense")

    def test_unknown_category_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("expense", {
                "category": "yacht", "description": "Staff note",
                "amount_kobo": 1000})


# ---------------------------------------------------------------------------
# 5. Unknown employees/accounts stay unresolved: no UUIDs invented.
# ---------------------------------------------------------------------------

class UnknownIdentityTests(unittest.TestCase):
    def test_handover_keeps_names_not_uuids(self):
        result = water_intake.extract_water_record(
            "Zubair handed 10000 cash to Ngozi")
        self.assertEqual(result["fields"]["from_name"], "Zubair")
        self.assertEqual(result["fields"]["to_name"], "Ngozi")
        for value in result["fields"].values():
            self.assertFalse(
                re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", str(value),
                    re.IGNORECASE),
                msg="extraction must never invent a UUID: %r" % (value,))

    def test_verified_rejects_message_names_as_ids(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("cash_handover", {
                "from_employee_id": "Michael",
                "to_employee_id": TO_ID, "amount_kobo": 8000000})

    def test_verified_allows_authoritative_uuids(self):
        checked = validate_verified("cash_handover", {
            "from_employee_id": FROM_ID,
            "to_employee_id": TO_ID, "amount_kobo": 8000000})
        self.assertEqual(checked["posting_type"], "cash_handover")


# ---------------------------------------------------------------------------
# 6. Verified validation for the new kinds (pure, no database).
# ---------------------------------------------------------------------------

class VerifiedValidationTests(unittest.TestCase):
    def test_handover_valid(self):
        checked = validate_verified("cash_handover", {
            "from_employee_id": FROM_ID, "to_employee_id": TO_ID,
            "amount_kobo": 8000000})
        self.assertEqual(checked["computed"]["amount_kobo"], 8000000)

    def test_self_handover_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("cash_handover", {
                "from_employee_id": FROM_ID, "to_employee_id": FROM_ID,
                "amount_kobo": 8000000})

    def test_handover_missing_party_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("cash_handover", {
                "from_employee_id": FROM_ID, "amount_kobo": 8000000})

    def test_deposit_valid(self):
        checked = validate_verified("bank_deposit", {
            "deposited_by": FROM_ID, "amount_kobo": 7000000,
            "destination_account": "Access Bank", "reference": "ABC123"})
        self.assertEqual(checked["computed"]["amount_kobo"], 7000000)

    def test_deposit_without_reference_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("bank_deposit", {
                "deposited_by": FROM_ID, "amount_kobo": 7000000,
                "destination_account": "Access Bank"})

    def test_deposit_without_destination_rejected(self):
        with self.assertRaises(MalformedVerifiedError):
            validate_verified("bank_deposit", {
                "deposited_by": FROM_ID, "amount_kobo": 7000000,
                "reference": "ABC123"})

    def test_new_kinds_mapped(self):
        self.assertEqual(review_posting_for_kind("cash_handover"),
            "cash_handover")
        self.assertEqual(review_posting_for_kind("bank_deposit"),
            "bank_deposit")
        self.assertEqual(
            operational_posting_for_kind("cash_handover"),
            ("cash_handover", "amose_post_cash_handover"))
        self.assertEqual(
            operational_posting_for_kind("bank_deposit"),
            ("bank_deposit", "amose_post_bank_deposit"))
        for kind in ("whatsapp_message", None, ""):
            with self.assertRaises(UnsupportedKindError):
                review_posting_for_kind(kind)
            with self.assertRaises(PostingUnsupportedKind):
                operational_posting_for_kind(kind)

    def test_review_error_classification(self):
        self.assertIsInstance(
            human_confirmation._classify_rpc_error(
                "CONFLICT: deposit reference ABC123 is already recorded"),
            RequestConflictError)
        self.assertIsInstance(
            human_confirmation._classify_rpc_error(
                "SCOPE: handover giver x is not assigned"),
            ScopeMismatchError)


# ---------------------------------------------------------------------------
# 7. Operator pay: preview only, never automatic.
# ---------------------------------------------------------------------------

class OperatorPayTests(unittest.TestCase):
    def test_rate_missing_reports_not_configured(self):
        preview = operator_pay.preview_operator_pay(250, None)
        self.assertEqual(preview["status"], "rate_not_configured")
        self.assertEqual(preview["message"], "rate not configured")
        self.assertIsNone(preview["amount_kobo"])

    def test_preview_calculation(self):
        preview = operator_pay.preview_operator_pay(250, 5000)
        self.assertEqual(preview["status"], "ok")
        self.assertEqual(preview["good_bags"], 250)
        self.assertEqual(preview["rate_kobo"], 5000)
        self.assertEqual(preview["amount_kobo"], 1250000)

    def test_preview_rejects_invalid_bags(self):
        with self.assertRaises(ValueError):
            operator_pay.preview_operator_pay(-1, 5000)
        with self.assertRaises(ValueError):
            operator_pay.preview_operator_pay(250.0, 5000)

    def test_preview_creates_nothing(self):
        # Pure function: no rest calls, no writes -- nothing to patch.
        preview = operator_pay.preview_operator_pay(0, 5000)
        self.assertEqual(preview["amount_kobo"], 0)

    def test_scope_preview_without_setting(self):
        async def fake_get(path, params=None):
            self.assertIn("biz_setting_versions", path)
            return []

        with patch("operator_pay.rest_get", side_effect=fake_get):
            preview = asyncio.run(
                operator_pay.preview_operator_pay_for_scope(
                    "tenant-1", "water", "asaba", 250))
        self.assertEqual(preview["status"], "rate_not_configured")

    def test_scope_preview_with_setting(self):
        async def fake_get(path, params=None):
            return [{"value": {"amount_kobo": 5000}}]

        with patch("operator_pay.rest_get", side_effect=fake_get):
            preview = asyncio.run(
                operator_pay.preview_operator_pay_for_scope(
                    "tenant-1", "water", "asaba", 250))
        self.assertEqual(preview["amount_kobo"], 1250000)

    def test_malformed_setting_is_missing(self):
        async def fake_get(path, params=None):
            return [{"value": {"amount_kobo": "lots"}}]

        with patch("operator_pay.rest_get", side_effect=fake_get):
            preview = asyncio.run(
                operator_pay.preview_operator_pay_for_scope(
                    "tenant-1", "water", "asaba", 250))
        self.assertEqual(preview["status"], "rate_not_configured")


# ---------------------------------------------------------------------------
# 8. Raw intake creates drafts only: zero operational records.
# ---------------------------------------------------------------------------

def _inbox_row(body, row_id="inbox-1"):
    return {"id": row_id, "provider": "whatsapp",
        "provider_account": "acct-1",
        "payload": {"kind": "message", "event": {
            "from": "+12025550123", "type": "text",
            "text": {"body": body}}}}


def _intake_client(inbox_rows):
    client = AsyncMock()

    async def fake_get(path, params=None):
        if path == "/rest/v1/biz_message_inbox":
            return httpx.Response(200, json=inbox_rows)
        if path == "/rest/v1/biz_sender_identities":
            return httpx.Response(200, json=[
                {"tenant_id": "tenant-1", "employee_id": "emp-1"}])
        if path == "/rest/v1/biz_assignments":
            return httpx.Response(200, json=[
                {"business_id": "amose_table_water",
                    "branch_id": "asaba"}])
        if path == "/rest/v1/biz_submissions":
            return httpx.Response(200, json=[{"id": SUBMISSION_ID}])
        if path == "/rest/v1/biz_outbound_messages":
            return httpx.Response(200, json=[])
        raise AssertionError("unexpected GET " + path)

    async def fake_post(path, json=None, params=None, headers=None):
        if path == "/rest/v1/rpc/amose_queue_review_requests":
            return httpx.Response(200, json={
                "status": "queued", "review_ref": REVIEW_REF,
                "submission_id": json["p_submission_id"],
                "submission_kind": "cash_handover",
                "request_key": json["p_request_key"],
                "notified": 1, "skipped": [], "is_retry": False})
        return httpx.Response(201)

    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(side_effect=fake_post)
    client.patch = AsyncMock(return_value=httpx.Response(204))
    return client


def _run_intake(client):
    with patch("message_processor.credentials",
               return_value=("https://example.supabase.co", "test-key")), \
         patch("message_processor.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        return asyncio.run(message_processor.process_inbox_batch())


OPERATIONAL_PATHS = (
    "/rest/v1/biz_production_runs",
    "/rest/v1/biz_inventory_movements",
    "/rest/v1/biz_sales",
    "/rest/v1/biz_payments",
    "/rest/v1/biz_expenses",
    "/rest/v1/biz_cash_custody_entries",
    "/rest/v1/brain_memories",
)


class RawIntakeCreatesNoRecordsTests(unittest.TestCase):
    def _submitted(self, client):
        calls = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(len(calls), 1)
        return calls[0].kwargs["json"][0]

    def test_handover_intake_is_draft_only(self):
        client = _intake_client(
            [_inbox_row("Michael handed 80000 cash to Vivian")])
        summary = _run_intake(client)
        self.assertEqual(summary["submitted"], 1)
        submitted = self._submitted(client)
        self.assertEqual(submitted["kind"], "cash_handover")
        self.assertEqual(submitted["status"], "draft")
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0].startswith("/rest/v1/rpc/amose_post_")
            or c.args[0].startswith("/rest/v1/rpc/amose_confirm")]
        self.assertEqual(rpc_posts, [])
        for path in OPERATIONAL_PATHS:
            hits = [c for c in client.post.call_args_list
                if c.args[0] == path]
            self.assertEqual(hits, [], msg=path)

    def test_deposit_intake_is_draft_only(self):
        client = _intake_client([_inbox_row(
            "Vivian deposited 70000 to Access Bank, reference ABC123")])
        summary = _run_intake(client)
        self.assertEqual(summary["submitted"], 1)
        submitted = self._submitted(client)
        self.assertEqual(submitted["kind"], "bank_deposit")
        self.assertEqual(submitted["status"], "draft")
        for path in OPERATIONAL_PATHS:
            hits = [c for c in client.post.call_args_list
                if c.args[0] == path]
            self.assertEqual(hits, [], msg=path)

    def test_each_kind_intakes_as_draft(self):
        bodies = {
            "Produced 250 good bags, 5 rejected": "production",
            "Sold 100 bags at 500 cash": "sale",
            "Received 50000 cash for sale YR-ABCD234EFG": "payment",
            "Spent 20000 on tricycle fuel cash": "expense",
        }
        for body, kind in bodies.items():
            client = _intake_client([_inbox_row(body)])
            summary = _run_intake(client)
            self.assertEqual(summary["submitted"], 1, msg=body)
            self.assertEqual(self._submitted(client)["kind"], kind,
                msg=body)
            self.assertEqual(self._submitted(client)["status"], "draft",
                msg=body)

    def test_water_kinds_trigger_no_rules(self):
        for kind in ("production", "sale", "payment", "expense",
                "cash_handover", "bank_deposit"):
            result = asyncio.run(rule_engine.apply_rules(
                "tenant-1", "amose_table_water", "asaba", "emp-1",
                "submission-1", {"kind": kind, "fields": {}}))
            self.assertEqual(result, {"applied": False}, msg=kind)


# ---------------------------------------------------------------------------
# 9. Confirmation posts exactly once (atomic); rejection posts nothing;
# retries are idempotent.
# ---------------------------------------------------------------------------

def _submission_row(kind="cash_handover", status="confirmed"):
    return {"id": SUBMISSION_ID, "tenant_id": "tenant-1",
        "business_id": "water", "branch_id": "asaba",
        "employee_id": "emp-1", "kind": kind, "status": status}


def _posting_client(kind, posting_ids, status="posted"):
    client = AsyncMock()
    client.get = AsyncMock(return_value=httpx.Response(
        200, json=[_submission_row(kind=kind)]))
    body = {"status": status, "posting_type": kind,
        "submission_id": SUBMISSION_ID, "is_retry": status != "posted",
        "brain_memory_id": "mem-1"}
    body.update(posting_ids)
    client.post = AsyncMock(return_value=httpx.Response(200, json=body))
    return client


def _run_posting(client, coro):
    with patch("operational_posting.credentials",
               return_value=("https://example.supabase.co", "test-key")), \
         patch("operational_posting.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        return asyncio.run(coro), client


class ConfirmationPostingTests(unittest.TestCase):
    def test_handover_confirmation_posts_once(self):
        client = _posting_client("cash_handover",
            {"cash_custody_entry_id": "cust-1"})
        result, client = _run_posting(client, post_submission(SUBMISSION_ID))
        self.assertEqual(result["status"], "posted")
        self.assertEqual(result["cash_custody_entry_id"], "cust-1")
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0] ==
            "/rest/v1/rpc/amose_post_cash_handover"]
        self.assertEqual(len(rpc_posts), 1)
        self.assertEqual(
            rpc_posts[0].kwargs["json"], {"p_submission_id": SUBMISSION_ID})

    def test_deposit_confirmation_posts_once(self):
        client = _posting_client("bank_deposit",
            {"cash_custody_entry_id": "cust-2"})
        result, client = _run_posting(client, post_submission(SUBMISSION_ID))
        self.assertEqual(result["posting_type"], "bank_deposit")
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0] ==
            "/rest/v1/rpc/amose_post_bank_deposit"]
        self.assertEqual(len(rpc_posts), 1)

    def test_retry_returns_original_without_duplicating(self):
        client = _posting_client("cash_handover",
            {"cash_custody_entry_id": "cust-1"}, status="already_posted")
        result, _client = _run_posting(client,
            post_submission(SUBMISSION_ID))
        self.assertEqual(result["status"], "already_posted")
        self.assertTrue(result["is_retry"])

    def test_posting_result_missing_custody_id_rejected(self):
        client = _posting_client("cash_handover", {})
        with self.assertRaises(PostingDatabaseError):
            _run_posting(client, post_submission(SUBMISSION_ID))

    def test_chat_confirm_posts_exactly_one_rpc(self):
        client = AsyncMock()
        body = {"status": "confirmed", "review_action": "confirmed",
            "posting_type": "cash_handover",
            "submission_kind": "cash_handover",
            "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
            "request_key": "k1",
            "verified_snapshot": {"from_employee_id": FROM_ID},
            "audit_id": "audit-1",
            "cash_custody_entry_id": "cust-1",
            "brain_memory_id": "mem-1"}
        client.post = AsyncMock(
            return_value=httpx.Response(200, json=body))
        result = asyncio.run(review_service.confirm_from_chat(
            client, REVIEW_REF, "whatsapp", "+12025550123", "k1"))
        self.assertEqual(result["status"], "confirmed")
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0].startswith("/rest/v1/rpc/")]
        self.assertEqual(len(rpc_posts), 1)
        self.assertEqual(rpc_posts[0].args[0],
            "/rest/v1/rpc/amose_review_confirm_command")

    def test_chat_reject_creates_no_operational_records(self):
        client = AsyncMock()
        body = {"status": "rejected", "review_action": "rejected",
            "submission_kind": "bank_deposit",
            "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
            "request_key": "k2", "reason": "duplicate report",
            "audit_id": "audit-9"}
        client.post = AsyncMock(
            return_value=httpx.Response(200, json=body))
        result = asyncio.run(review_service.reject_from_chat(
            client, REVIEW_REF, "whatsapp", "+12025550123",
            "duplicate report", "k2"))
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("cash_custody_entry_id", result)
        self.assertNotIn("brain_memory_id", result)
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0].startswith("/rest/v1/rpc/")]
        self.assertEqual(len(rpc_posts), 1)
        self.assertTrue(
            rpc_posts[0].args[0].endswith("amose_reject_submission"))

    def test_new_kinds_are_reviewable(self):
        self.assertIn("cash_handover", review_service.REVIEWABLE_KINDS)
        self.assertIn("bank_deposit", review_service.REVIEWABLE_KINDS)

    def test_draft_handover_never_posts(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=httpx.Response(
            200, json=[_submission_row(kind="cash_handover",
                status="draft")]))
        client.post = AsyncMock()
        with self.assertRaises(
                operational_posting.UnconfirmedSubmissionError):
            _run_posting(client, post_submission(SUBMISSION_ID))
        client.post.assert_not_called()

    def test_draft_deposit_never_posts(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=httpx.Response(
            200, json=[_submission_row(kind="bank_deposit",
                status="draft")]))
        client.post = AsyncMock()
        with self.assertRaises(
                operational_posting.UnconfirmedSubmissionError):
            _run_posting(client, post_submission(SUBMISSION_ID))
        client.post.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Business-scope regression: water_intake runs ONLY for the AMOSE
# water-business scope (amose_table_water asaba/warri). Nughe Farms Warri
# keeps poultry extraction; Phone Center, unknown, and future businesses
# keep the previous generic sale parsing behavior exactly.
# ---------------------------------------------------------------------------

class BusinessScopeRegressionTests(unittest.TestCase):
    def test_amose_asaba_uses_water_intake(self):
        extraction = rule_engine.extract("amose_table_water", "asaba",
            "Michael handed 80000 cash to Vivian")
        self.assertEqual(extraction["kind"], "cash_handover")

    def test_amose_warri_uses_water_intake(self):
        extraction = rule_engine.extract("amose_table_water", "warri",
            "Vivian deposited 70000 to Access Bank, reference ABC123")
        self.assertEqual(extraction["kind"], "bank_deposit")
        production = rule_engine.extract("amose_table_water", "warri",
            "Produced 250 good bags, 5 rejected")
        self.assertEqual(production["kind"], "production")

    def test_nughe_farms_warri_keeps_poultry(self):
        extraction = rule_engine.extract("nughe_farms", "warri",
            "26 crates today", received_at="2026-09-16T10:00:00Z")
        self.assertEqual(extraction["kind"], "poultry_daily_report")
        self.assertEqual(extraction["fields"]["crates"], 26.0)

    def test_phone_center_ignores_water_production(self):
        extraction = rule_engine.extract("phone_center", "warri",
            "Produced 250 good bags, 5 rejected")
        self.assertNotEqual(extraction["kind"], "production")
        self.assertEqual(extraction["kind"], "whatsapp_message")

    def test_phone_center_ignores_cash_handover(self):
        extraction = rule_engine.extract("phone_center", "warri",
            "Michael handed 80000 cash to Vivian")
        self.assertNotEqual(extraction["kind"], "cash_handover")
        self.assertEqual(extraction["kind"], "whatsapp_message")

    def test_phone_center_ignores_bank_deposit(self):
        extraction = rule_engine.extract("phone_center", "warri",
            "Vivian deposited 70000 to Access Bank, reference ABC123")
        self.assertNotEqual(extraction["kind"], "bank_deposit")
        self.assertEqual(extraction["kind"], "whatsapp_message")

    def test_unknown_business_gets_no_amose_behavior(self):
        for body, forbidden in (
                ("Produced 250 good bags, 5 rejected", "production"),
                ("Received 50000 cash for sale YR-ABCD234EFG", "payment"),
                ("Spent 20000 on tricycle fuel cash", "expense"),
                ("Michael handed 80000 cash to Vivian", "cash_handover"),
                ("Vivian deposited 70000 to Access Bank, reference ABC123",
                    "bank_deposit")):
            extraction = rule_engine.extract("future_biz", "hq", body)
            self.assertNotEqual(extraction["kind"], forbidden, msg=body)

    def test_generic_sale_unchanged_outside_amose(self):
        for scope in (("phone_center", "warri"), ("future_biz", "hq"),
                ("nughe_farms", "asaba")):
            extraction = rule_engine.extract(
                scope[0], scope[1], "Sold 50 bags at 500")
            self.assertEqual(extraction["kind"], "sale", msg=str(scope))
            self.assertEqual(extraction["intent"], "sale", msg=str(scope))
            self.assertEqual(extraction["fields"]["quantity"], 50.0,
                msg=str(scope))
            self.assertEqual(extraction["fields"]["unit"], "bag",
                msg=str(scope))
            self.assertEqual(extraction["fields"]["unit_price"], 500.0,
                msg=str(scope))
            self.assertEqual(extraction["missing_fields"], ["currency"],
                msg=str(scope))
            self.assertEqual(extraction["errors"], [], msg=str(scope))
            self.assertNotIn("provenance", extraction, msg=str(scope))


if __name__ == "__main__":
    unittest.main()
