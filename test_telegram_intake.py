"""Telegram full staff-report intake tests.

Covers every required report type (SALE, PRODUCTION, EXPENSE, DEPOSIT,
STOCK, CUSTOMER PAYMENT, CUSTOMER DEBT), unlinked senders, cross-scope
refusal, duplicates/retries, malformed and incomplete input,
confirmation/cancellation/review regression, and the no-secret-leak and
channel-boundary guarantees.

Every database and Telegram API interaction is mocked: no test contacts
a real service or reveals a real secret.
"""
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import human_confirmation
import review_service as review_service_module
import outbound_telegram as outbound_telegram_module
import telegram_adapter as tg
import telegram_intake as intake

CHAT_ID = "123456789"
REF = "YR-ABCD234EFG"
SUBMISSION_UUID = "11111111-1111-1111-1111-111111111111"
TENANT_UUID = "00000000-0000-0000-0000-000000000001"
EMP_UUID = "22222222-2222-2222-2222-222222222222"
BUSINESS_ID = "amose_table_water"
BRANCH_ID = "asaba"

EXAMPLES = {
    "SALE": "SALE 50 bags at 500 cash",
    "PRODUCTION": "PRODUCTION 225 bags used 7kg nylon",
    "EXPENSE": "EXPENSE fuel 15000",
    "DEPOSIT": "DEPOSIT 80000 bank transfer",
    "STOCK": "STOCK 120 normal bags and 75 cold bags",
    "CUSTOMER PAYMENT": "CUSTOMER PAYMENT Emeka 25000 transfer",
    "CUSTOMER DEBT": "CUSTOMER DEBT Ada 12000",
}

EXPECTED_KINDS = {
    "SALE": "sale",
    "PRODUCTION": "production",
    "EXPENSE": "expense",
    "DEPOSIT": "bank_deposit",
    "STOCK": "stock",
    "CUSTOMER PAYMENT": "customer_payment",
    "CUSTOMER DEBT": "customer_debt",
}


def message_update(update_id=100, text="hello", chat_id=CHAT_ID,
        sender_id=None):
    sender = int(sender_id if sender_id is not None else chat_id)
    return {"update_id": update_id,
        "message": {"message_id": 1,
            "from": {"id": sender, "first_name": "Attacker Chosen Name",
                "username": "spoofed_name"},
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text}}


class DetectCommandTests(unittest.TestCase):
    def test_all_seven_examples_detected(self):
        for command, text in EXAMPLES.items():
            with self.subTest(command=command):
                found, remainder = intake.detect_command(text)
                self.assertEqual(found, command)
                self.assertTrue(remainder)

    def test_case_insensitive_and_branch_prefix_tolerated(self):
        found, remainder = intake.detect_command(
            "warri: sale 50 bags at 500 cash")
        self.assertEqual(found, "SALE")
        self.assertIn("50 bags", remainder)
        found, _ = intake.detect_command("customer payment Emeka 25000")
        self.assertEqual(found, "CUSTOMER PAYMENT")

    def test_non_reports_not_detected(self):
        for text in ("hello", "REVIEW CONFIRM %s KEY k" % REF,
                "Sold 50 bags at 500", "", "CUSTOMER Ada 12000"):
            with self.subTest(text=text):
                found, _ = intake.detect_command(text)
                self.assertIsNone(found)
        found, _ = intake.detect_command(None)
        self.assertIsNone(found)


class NormalizeTests(unittest.TestCase):
    def test_shared_normalization_shapes(self):
        normalized, _, _ = intake.normalize_for_shared(
            "SALE", "50 bags at 500 cash")
        self.assertEqual(normalized, "Sold 50 bags at 500 cash")
        normalized, _, _ = intake.normalize_for_shared(
            "PRODUCTION", "225 bags used 7kg nylon")
        self.assertEqual(normalized, "Produced 225 bags used 7kg nylon")
        normalized, _, _ = intake.normalize_for_shared(
            "EXPENSE", "fuel 15000")
        self.assertEqual(normalized, "Spent fuel 15000")
        normalized, _, _ = intake.normalize_for_shared(
            "DEPOSIT", "80000 bank transfer")
        self.assertEqual(normalized, "deposited 80000 bank transfer")

    def test_customer_payment_parses_directly(self):
        result = intake.parse_customer_payment("Emeka 25000 transfer")
        self.assertEqual(result["kind"], "customer_payment")
        self.assertEqual(result["fields"]["customer_name"], "Emeka")
        self.assertEqual(result["fields"]["amount_kobo"], 2500000)
        self.assertEqual(result["fields"]["method"], "transfer")
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["errors"], [])
        incomplete = intake.parse_customer_payment("Emeka")
        self.assertIn("amount_kobo", incomplete["missing_fields"])
        self.assertIn("method", incomplete["missing_fields"])
        self.assertTrue(incomplete["errors"])


class DirectParserTests(unittest.TestCase):
    def test_stock_example(self):
        result = intake.parse_stock("120 normal bags and 75 cold bags")
        self.assertEqual(result["kind"], "stock")
        self.assertEqual(result["fields"]["normal_quantity"], 120)
        self.assertEqual(result["fields"]["cold_quantity"], 75)
        self.assertEqual(result["fields"]["total_quantity"], 195)
        self.assertEqual(result["fields"]["unit"], "bag")
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["errors"], [])

    def test_stock_single_count_lists_split_as_missing(self):
        result = intake.parse_stock("200 bags")
        self.assertEqual(result["kind"], "stock")
        self.assertEqual(result["fields"]["total_quantity"], 200)
        self.assertIn("normal_quantity", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_stock_garbage_names_missing(self):
        result = intake.parse_stock("a lot of bags maybe")
        self.assertEqual(result["kind"], "stock")
        self.assertIn("normal_quantity", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_customer_debt_example(self):
        result = intake.parse_customer_debt("Ada 12000")
        self.assertEqual(result["kind"], "customer_debt")
        self.assertEqual(result["fields"]["customer_name"], "Ada")
        self.assertEqual(result["fields"]["amount_kobo"], 1200000)
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["errors"], [])

    def test_customer_debt_missing(self):
        result = intake.parse_customer_debt("")
        self.assertEqual(result["kind"], "customer_debt")
        self.assertIn("customer_name", result["missing_fields"])
        self.assertIn("amount_kobo", result["missing_fields"])


class PreviewTests(unittest.TestCase):
    def test_preview_carries_confirm_correct_cancel(self):
        extraction = {"kind": "sale", "fields": {"quantity": 50.0,
            "unit": "bag", "unit_price": 500.0}, "missing_fields": [],
            "errors": []}
        text = intake.format_intake_preview(
            extraction, review_ref=REF, request_key="tgintake:inbox-1")
        self.assertIn("Recorded", text)
        self.assertIn(REF, text)
        self.assertIn("REVIEW CONFIRM %s KEY tgintake:inbox-1" % REF, text)
        self.assertIn("CORRECTION", text)
        self.assertIn("REVIEW REJECT", text)

    def test_preview_names_missing_and_manual_note(self):
        extraction = {"kind": "stock",
            "fields": {"total_quantity": 200, "unit": "bag"},
            "missing_fields": ["normal_quantity", "cold_quantity"],
            "errors": ["split not stated"]}
        text = intake.format_intake_preview(
            extraction, queue_note="Administrator review pending.")
        self.assertIn("Missing: normal_quantity, cold_quantity", text)
        self.assertIn("Administrator review pending.", text)


class IntakeFlowHarness(unittest.IsolatedAsyncioTestCase):
    async def _run_text(self, text, identity_rows=None, assignments=None,
            queue_telegram=None, update_id=100, inbox_id="inbox-1",
            fail_assignments=False):
        identity_rows = identity_rows if identity_rows is not None else [
            {"tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                "business_id": BUSINESS_ID, "branch_id": BRANCH_ID}]
        assignments = assignments if assignments is not None else [
            {"business_id": BUSINESS_ID, "branch_id": BRANCH_ID}]
        posted = []

        client = AsyncMock()

        async def fake_get(path, params=None):
            if fail_assignments and path == "/rest/v1/biz_assignments":
                raise httpx.HTTPError("db down")
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=list(identity_rows))
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=list(assignments))
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": SUBMISSION_UUID}])
            return httpx.Response(200, json=[])

        async def fake_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/biz_submissions":
                posted.extend(json or [])
                return httpx.Response(201, json={})
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                return httpx.Response(200, json={"status": "queued",
                    "review_ref": REF,
                    "submission_id": json["p_submission_id"],
                    "submission_kind": "sale",
                    "request_key": json["p_request_key"],
                    "notified": 1, "skipped": [], "is_retry": False})
            return httpx.Response(201, json={})

        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(side_effect=fake_post)
        client.patch = AsyncMock(return_value=httpx.Response(204))

        async def default_queue(submission_id, request_key):
            return {"status": "queued", "review_ref": REF,
                "submission_id": submission_id, "request_key": request_key,
                "notified": [EMP_UUID], "skipped": [], "is_retry": False}

        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            update_id=update_id, text=text))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "queue_telegram_reviews",
                new_callable=AsyncMock) as queue_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            queue_mock.side_effect = queue_telegram if queue_telegram \
                is not None else default_queue
            outcome = await tg.process_stored_update(
                update, inbox_id, summary)
        return outcome, summary, client, queue_mock, send_mock, posted


class ReportTypeTests(IntakeFlowHarness):
    async def test_sale_intake_end_to_end(self):
        outcome, summary, client, queue_mock, send_mock, posted = \
            await self._run_text(EXAMPLES["SALE"])
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(outcome["kind"], "sale")
        self.assertEqual(summary["intakes_submitted"], 1)
        submissions = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(len(submissions), 1)
        row = posted[0]
        self.assertEqual(row["kind"], "sale")
        self.assertEqual(row["provider"], "telegram")
        self.assertEqual(row["provider_account"], tg.BOT_ACCOUNT)
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["tenant_id"], TENANT_UUID)
        self.assertEqual(row["employee_id"], EMP_UUID)
        self.assertEqual(row["business_id"], BUSINESS_ID)
        self.assertEqual(row["branch_id"], BRANCH_ID)
        self.assertEqual(row["idempotency_key"], "inbox-1")
        queue_mock.assert_called_once_with(
            SUBMISSION_UUID, "tgintake:inbox-1")
        whatsapp = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/rpc/amose_queue_review_requests"]
        self.assertEqual(len(whatsapp), 1)
        self.assertEqual(
            whatsapp[0].kwargs["json"]["p_request_key"], "queuereq:inbox-1")
        reply = send_mock.call_args.args[1]
        self.assertIn("Recorded", reply)
        self.assertIn(REF, reply)
        self.assertIn("REVIEW CONFIRM", reply)
        self.assertIn("CORRECTION", reply)
        self.assertIn("REVIEW REJECT", reply)
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})

    async def test_all_seven_types_create_matching_drafts(self):
        for command, text in EXAMPLES.items():
            with self.subTest(command=command):
                # Every kind queues a review request and gets its parsed
                # report shown back with Confirm / Correct / Cancel
                # guidance carrying the issued reference.
                outcome, summary, _, _, send_mock, posted = \
                    await self._run_text(text)
                self.assertEqual(outcome["outcome"], "intake")
                self.assertEqual(outcome["kind"], EXPECTED_KINDS[command])
                self.assertTrue(posted)
                self.assertEqual(posted[0]["kind"], EXPECTED_KINDS[command])
                self.assertEqual(summary["intakes_submitted"], 1)
                reply = send_mock.call_args.args[1]
                self.assertIn("Recorded", reply)
                self.assertIn(REF, reply)
                self.assertIn("REVIEW CONFIRM", reply)

    async def test_production_reuses_shared_extraction_shape(self):
        _, _, _, _, _, posted = await self._run_text(
            EXAMPLES["PRODUCTION"])
        parsed = posted[0]["payload"]["parsed"]
        self.assertEqual(posted[0]["kind"], "production")
        self.assertEqual(parsed["fields"]["good_quantity"], 225)
        self.assertEqual(
            parsed["provenance"]["good_quantity"], "staff_reported")

    async def test_customer_payment_keeps_customer_name(self):
        _, _, _, _, send_mock, posted = await self._run_text(
            EXAMPLES["CUSTOMER PAYMENT"])
        parsed = posted[0]["payload"]["parsed"]
        self.assertEqual(posted[0]["kind"], "customer_payment")
        self.assertEqual(parsed["fields"]["customer_name"], "Emeka")
        self.assertEqual(parsed["fields"]["amount_kobo"], 2500000)
        self.assertEqual(parsed["fields"]["method"], "transfer")
        self.assertEqual(parsed["missing_fields"], [])
        reply = send_mock.call_args.args[1]
        self.assertIn("Emeka", reply)

    async def test_customer_debt_records_structured_draft(self):
        outcome, _, _, _, send_mock, posted = await self._run_text(
            EXAMPLES["CUSTOMER DEBT"])
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(posted[0]["kind"], "customer_debt")
        parsed = posted[0]["payload"]["parsed"]
        self.assertEqual(parsed["fields"]["customer_name"], "Ada")
        self.assertEqual(parsed["fields"]["amount_kobo"], 1200000)
        reply = send_mock.call_args.args[1]
        self.assertIn("Ada", reply)
        self.assertIn(REF, reply)


class AuthorizationTests(IntakeFlowHarness):
    async def test_unlinked_sender_gets_linking_help_no_draft(self):
        outcome, summary, client, queue_mock, send_mock, posted = \
            await self._run_text(EXAMPLES["SALE"], identity_rows=[])
        self.assertEqual(outcome["outcome"], "help")
        self.assertEqual(posted, [])
        queue_mock.assert_not_called()
        reply = send_mock.call_args.args[1]
        self.assertIn("not linked", reply.lower())
        self.assertIn("SALE 50 bags at 500 cash", reply)
        self.assertEqual(summary["intake_unlinked"], 1)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})

    async def test_multibranch_sender_needs_prefix(self):
        outcome, summary, client, queue_mock, send_mock, posted = \
            await self._run_text(EXAMPLES["SALE"], assignments=[
                {"business_id": BUSINESS_ID, "branch_id": "asaba"},
                {"business_id": BUSINESS_ID, "branch_id": "warri"}])
        self.assertEqual(outcome["outcome"], "help")
        self.assertEqual(posted, [])
        queue_mock.assert_not_called()
        reply = send_mock.call_args.args[1]
        self.assertIn("WARRI", reply)
        self.assertEqual(summary["intake_clarifications"], 1)

    async def test_multibranch_prefix_routes_to_named_branch(self):
        outcome, _, _, _, _, posted = await self._run_text(
            "WARRI: " + EXAMPLES["SALE"], assignments=[
                {"business_id": BUSINESS_ID, "branch_id": "asaba"},
                {"business_id": BUSINESS_ID, "branch_id": "warri"}])
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(posted[0]["branch_id"], "warri")

    async def test_foreign_prefix_cannot_escape_own_assignment(self):
        outcome, _, _, _, _, posted = await self._run_text(
            "WARRI: " + EXAMPLES["SALE"])
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(posted[0]["business_id"], BUSINESS_ID)
        self.assertEqual(posted[0]["branch_id"], BRANCH_ID)
        self.assertEqual(posted[0]["tenant_id"], TENANT_UUID)

    async def test_display_name_spoof_never_becomes_identity(self):
        outcome, _, client, _, _, posted = await self._run_text(
            EXAMPLES["SALE"])
        self.assertEqual(outcome["outcome"], "intake")
        identity_params = client.get.call_args_list[0].kwargs["params"]
        self.assertEqual(
            identity_params["provider_sender"], "eq." + CHAT_ID)
        self.assertEqual(posted[0]["payload"]["sender_phone"], CHAT_ID)

    async def test_assignment_lookup_failure_fails_closed(self):
        outcome, summary, client, _, send_mock, posted = \
            await self._run_text(EXAMPLES["SALE"], fail_assignments=True)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(posted, [])
        send_mock.assert_not_called()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(
            client.patch.call_args.kwargs["json"]["status"], "failed")


class MalformedIncompleteTests(IntakeFlowHarness):
    async def test_unrecognized_text_creates_no_draft(self):
        outcome, summary, client, queue_mock, send_mock, posted = \
            await self._run_text("hello there")
        self.assertEqual(outcome["outcome"], "help")
        self.assertEqual(posted, [])
        queue_mock.assert_not_called()
        reply = send_mock.call_args.args[1]
        self.assertIn("SALE", reply)
        self.assertEqual(summary["intake_unsupported"], 1)

    async def test_bare_sale_asks_for_missing(self):
        outcome, _, _, _, send_mock, posted = await self._run_text("SALE")
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(posted[0]["kind"], "sale")
        reply = send_mock.call_args.args[1]
        self.assertIn("Missing:", reply)
        self.assertIn("quantity", reply)

    async def test_incomplete_deposit_asks_for_missing(self):
        outcome, _, _, _, send_mock, posted = await self._run_text(
            EXAMPLES["DEPOSIT"])
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(posted[0]["kind"], "bank_deposit")
        reply = send_mock.call_args.args[1]
        self.assertIn("Missing:", reply)
        self.assertIn("reference", reply)


class RetryIdempotencyTests(IntakeFlowHarness):
    async def test_retry_reuses_same_queue_keys(self):
        keys = []

        async def recording(submission_id, request_key):
            keys.append(request_key)
            return {"status": "already_queued", "review_ref": REF,
                "submission_id": submission_id, "request_key": request_key,
                "notified": [], "skipped": [], "is_retry": True}

        for _ in range(2):
            outcome, _, client, _, _, _ = await self._run_text(
                EXAMPLES["SALE"], queue_telegram=recording)
            self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(keys, ["tgintake:inbox-1", "tgintake:inbox-1"])
        whatsapp = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/rpc/amose_queue_review_requests"]
        for call in whatsapp:
            self.assertEqual(
                call.kwargs["json"]["p_request_key"], "queuereq:inbox-1")

    async def test_queue_outage_keeps_draft_and_marks_processed(self):
        async def down(submission_id, request_key):
            raise human_confirmation.WorkflowDatabaseError("db down")

        async def whatsapp_down(client, submission_id, request_key):
            raise human_confirmation.WorkflowDatabaseError("db down")

        with patch.object(review_service_module, "queue_review_requests",
                side_effect=whatsapp_down):
            outcome, summary, client, _, send_mock, posted = \
                await self._run_text(EXAMPLES["SALE"], queue_telegram=down)
        self.assertEqual(outcome["outcome"], "intake")
        self.assertTrue(posted)
        self.assertEqual(summary["intake_queue_failed"], 2)
        reply = send_mock.call_args.args[1]
        self.assertIn("administrator", reply.lower())
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})


class ReviewRegressionTests(IntakeFlowHarness):
    async def _run_command(self, text, result):
        client = AsyncMock()
        client.patch = AsyncMock(return_value=httpx.Response(204))
        summary = tg._new_summary()
        update = tg.parse_update(message_update(text=text))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "handle_review_command",
                new_callable=AsyncMock, return_value=result) as command, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        return outcome, summary, client, command, send_mock

    async def test_review_confirm_unchanged(self):
        outcome, summary, client, command, send_mock = \
            await self._run_command(
                "REVIEW CONFIRM %s KEY k1" % REF,
                {"outcome": "confirm", "result": {}})
        self.assertEqual(outcome["outcome"], "confirm")
        command.assert_called_once()
        row = command.call_args.args[1]
        self.assertEqual(row, {"id": "inbox-9", "provider": "telegram"})
        self.assertEqual(command.call_args.args[2], CHAT_ID)
        reply = send_mock.call_args.args[1]
        self.assertIn(REF, reply)

    async def test_review_reject_cancels(self):
        outcome, _, _, command, send_mock = await self._run_command(
            "REVIEW REJECT %s KEY k1 REASON wrong figures" % REF,
            {"outcome": "reject", "result": {}})
        self.assertEqual(outcome["outcome"], "reject")
        command.assert_called_once()
        reply = send_mock.call_args.args[1]
        self.assertIn(REF, reply)

    async def test_malformed_review_prefix_still_refused(self):
        client_posts = []

        async def refuse(client, row, summary):
            summary["reviews_refused"] += 1
            return {"outcome": "refused", "error": "RefusedReviewCommand"}

        client = AsyncMock()
        client.patch = AsyncMock(return_value=httpx.Response(204))
        client.post = AsyncMock(side_effect=lambda *a, **k: client_posts.append(
            (a, k)) or httpx.Response(201, json={}))
        summary = tg._new_summary()
        update = tg.parse_update(
            message_update(text="REVIEW CONFIRM junk KEY k1"))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "refuse_malformed_command",
                side_effect=refuse), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        self.assertEqual(outcome["outcome"], "refused")
        self.assertEqual(
            [c for c in client_posts
                if c[0][0] == "/rest/v1/biz_submissions"], [])


class HelpTextTests(unittest.TestCase):
    def test_start_and_help_show_intake_examples(self):
        self.assertIn("SALE 50 bags at 500 cash", tg.HELP_UNLINKED)
        self.assertIn("SALE 50 bags at 500 cash", tg.HELP_LINKED)
        self.assertIn("SALE 50 bags at 500 cash", tg.HELP_LINKED_ORDINARY)
        self.assertIn("CUSTOMER DEBT Ada 12000", tg.HELP_LINKED_ORDINARY)
        self.assertIn("STOCK 120 normal bags and 75 cold bags",
            tg.HELP_LINKED)
        self.assertIn("REVIEW CONFIRM", tg.HELP_LINKED_ORDINARY)


class SafetyTests(IntakeFlowHarness):
    async def test_replies_and_notes_carry_no_secrets(self):
        token = "tok-SECRET-value"
        outcome, _, client, _, send_mock, _ = await self._run_text(
            EXAMPLES["SALE"])
        self.assertEqual(outcome["outcome"], "intake")
        reply = send_mock.call_args.args[1]
        for secretish in (token, "YAMSI_API_KEY", tg.BOT_ACCOUNT + "-secret"):
            self.assertNotIn(secretish, reply)
        note = client.patch.call_args.kwargs["json"]
        self.assertNotIn(CHAT_ID, str(note))
        self.assertNotIn(token, str(note))

    def test_channel_boundary_holds_with_intake(self):
        import pathlib
        root = pathlib.Path(__file__).parent
        sources = {name: (root / name).read_text(encoding="utf-8")
            for name in ("telegram_adapter.py", "telegram_webhook.py",
                "outbound_telegram.py")}
        for name, text in sources.items():
            for forbidden in ("amose_post_", "operational_posting",
                    "INSERT INTO", "biz_production", "biz_sales",
                    "brain_memory", "outbound_whatsapp", "whatsapp_webhook",
                    "WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN",
                    "WHATSAPP_VERIFY_TOKEN"):
                self.assertNotIn(forbidden, text,
                    "%s must not contain %s" % (name, forbidden))
        import re
        rpcs = set(re.findall(r'"(amose_[a-z_]+)"', sources[
            "telegram_adapter.py"]))
        allowed = {"amose_issue_telegram_link",
            "amose_consume_telegram_link",
            "amose_queue_telegram_review_requests",
            "amose_mint_telegram_callback",
            "amose_consume_telegram_callback",
            "amose_review_confirm_command", "amose_reject_submission",
            "amose_confirm_submission"}
        for rpc in rpcs:
            self.assertIn(rpc, allowed, "unexpected RPC: " + rpc)


if __name__ == "__main__":
    unittest.main()
