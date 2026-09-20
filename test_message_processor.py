import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from app import app
from message_processor import parse_message, process_inbox_batch
from message_processor import _split_branch_prefix, _match_assignment
from message_processor import _resolve_multi_assignment, _clarification_text

MESSAGE_EVENT = {"from": "+12025550123", "type": "text", "text": {"body": "Sold 50 bags at 500"}}

SUBMISSION_UUID = "11111111-1111-1111-1111-111111111111"
REVIEW_REF = "YR-ABCD234EFG"


def inbox_row(row_id="inbox-1", event=None, kind="message"):
    return {"id": row_id, "provider": "whatsapp", "provider_account": "acct-1",
        "payload": {"kind": kind, "event": event if event is not None else MESSAGE_EVENT}}


class ParseMessageTests(unittest.TestCase):
    def test_full_sale(self):
        result = parse_message("Sold 50 bags at 500")
        self.assertEqual(result["intent"], "sale")
        self.assertEqual(result["fields"]["quantity"], 50.0)
        self.assertEqual(result["fields"]["unit"], "bag")
        self.assertEqual(result["fields"]["unit_price"], 500.0)
        self.assertEqual(result["missing_fields"], ["currency"])
        self.assertEqual(result["errors"], [])

    def test_sale_with_currency(self):
        result = parse_message("Sold 50 bags at 500 NGN")
        self.assertEqual(result["fields"]["currency"], "NGN")
        self.assertNotIn("currency", result["missing_fields"])

    def test_incomplete_sale(self):
        result = parse_message("Sold 50 bags")
        self.assertEqual(result["intent"], "sale")
        self.assertEqual(result["fields"]["quantity"], 50.0)
        self.assertIn("unit_price", result["missing_fields"])
        self.assertTrue(result["errors"])

    def test_unrecognized_message(self):
        result = parse_message("Good morning, please call me")
        self.assertIsNone(result["intent"])
        self.assertTrue(result["errors"])

    def test_empty_message(self):
        result = parse_message("")
        self.assertIsNone(result["intent"])
        self.assertEqual(result["errors"], ["Empty or missing message text"])


class BranchPrefixTests(unittest.TestCase):
    PAIRS = [("water", "asaba"), ("water", "warri")]

    def test_split_prefix(self):
        self.assertEqual(_split_branch_prefix("ASABA: Sold 100 bags"), ("ASABA", "Sold 100 bags"))
        self.assertEqual(_split_branch_prefix("aSaBa : Sold 10 bags"), ("aSaBa", "Sold 10 bags"))
        self.assertEqual(_split_branch_prefix("ASABA:"), ("ASABA", ""))
        self.assertEqual(_split_branch_prefix("hello world"), (None, "hello world"))
        self.assertEqual(_split_branch_prefix(""), (None, ""))
        self.assertEqual(_split_branch_prefix(None), (None, None))

    def test_match_assignment(self):
        self.assertEqual(_match_assignment(self.PAIRS, "asaba"), ("water", "asaba"))
        self.assertEqual(_match_assignment(self.PAIRS, "WARRI"), ("water", "warri"))
        self.assertIsNone(_match_assignment(self.PAIRS, "lagos"))
        self.assertIsNone(_match_assignment(self.PAIRS, ""))
        self.assertIsNone(_match_assignment(self.PAIRS, None))
        ambiguous = [("water", "warri"), ("phone_center", "warri")]
        self.assertIsNone(_match_assignment(ambiguous, "warri"))

    def test_resolve_multi_assignment(self):
        resolved = _resolve_multi_assignment(self.PAIRS, "WARRI: Produced 200 bags")
        self.assertEqual(resolved, (("water", "warri"), "Produced 200 bags"))
        self.assertIsNone(_resolve_multi_assignment(self.PAIRS, "Sold 50 bags"))
        self.assertIsNone(_resolve_multi_assignment(self.PAIRS, "LAGOS: Sold 50 bags"))
        self.assertIsNone(_resolve_multi_assignment(self.PAIRS, "ASABA:"))
        self.assertIsNone(_resolve_multi_assignment(self.PAIRS, "ASABA:   "))
        self.assertIsNone(_resolve_multi_assignment(self.PAIRS, None))

    def test_clarification_text(self):
        self.assertEqual(_clarification_text(self.PAIRS),
            "Please begin your message with ASABA: or WARRI: so YAMSI knows which branch to use.")
        self.assertEqual(_clarification_text(
                [("water", "asaba"), ("water", "warri"), ("water", "aba")]),
            "Please begin your message with ABA:, ASABA:, or WARRI: so YAMSI knows which branch to use.")


class ProcessInboxTests(unittest.TestCase):
    def _client(self, identities, assignments, post_status=201, patch_status=204, outbound_existing=None):
        client = AsyncMock()
        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_message_inbox":
                return httpx.Response(200, json=self._inbox_rows)
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=identities)
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=assignments)
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": SUBMISSION_UUID}])
            if path == "/rest/v1/biz_outbound_messages":
                return httpx.Response(200, json=list(outbound_existing or []))
            raise AssertionError("unexpected GET " + path)
        async def fake_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                return httpx.Response(200, json={"status": "queued",
                    "review_ref": REVIEW_REF,
                    "submission_id": json["p_submission_id"],
                    "submission_kind": "sale",
                    "request_key": json["p_request_key"],
                    "notified": 1, "skipped": [], "is_retry": False})
            return httpx.Response(post_status)
        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(side_effect=fake_post)
        client.patch = AsyncMock(return_value=httpx.Response(patch_status))
        return client

    def _run(self, inbox_rows, identities, assignments, post_status=201, patch_status=204, outbound_existing=None):
        self._inbox_rows = inbox_rows
        client = self._client(identities, assignments, post_status, patch_status, outbound_existing)
        with patch("message_processor.credentials", return_value=("https://example.supabase.co", "test-key")), \
             patch("message_processor.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            summary = asyncio.run(process_inbox_batch())
        return summary, client

    # 1. known sender + valid sale message
    def test_known_sender_valid_sale(self):
        summary, client = self._run(
            [inbox_row()],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}])
        self.assertEqual(summary, {"scanned": 1, "submitted": 1, "unmatched": 0, "skipped_non_message": 0,
            "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0,
            "reviews_confirmed": 0, "reviews_rejected": 0, "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 1, "review_requests_failed": 0,
            "review_requests_already_queued": 0, "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0})
        submitted = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"][0].kwargs["json"][0]
        self.assertEqual(submitted["status"], "draft")
        self.assertEqual(submitted["tenant_id"], "tenant-1")
        self.assertEqual(submitted["business_id"], "water")
        self.assertEqual(submitted["branch_id"], "warri")
        self.assertEqual(submitted["employee_id"], "emp-1")
        self.assertEqual(submitted["inbox_id"], "inbox-1")
        self.assertEqual(submitted["payload"]["parsed"]["fields"]["quantity"], 50.0)
        client.patch.assert_called_once()
        self.assertEqual(client.patch.call_args.kwargs["json"], {"status": "processed"})

    # 2. unknown sender
    def test_unknown_sender(self):
        summary, client = self._run([inbox_row()], [], [])
        self.assertEqual(summary, {"scanned": 1, "submitted": 0, "unmatched": 1, "skipped_non_message": 0,
            "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0,
            "reviews_confirmed": 0, "reviews_rejected": 0, "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0, "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0})
        client.post.assert_not_called()
        self.assertEqual(client.patch.call_args.kwargs["json"], {"status": "unmatched"})

    # 3. incomplete message
    def test_incomplete_message(self):
        event = {"from": "+12025550123", "type": "text", "text": {"body": "Sold 50 bags"}}
        summary, client = self._run(
            [inbox_row(event=event)],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}])
        self.assertEqual(summary["submitted"], 1)
        submitted = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"][0].kwargs["json"][0]
        self.assertIn("unit_price", submitted["payload"]["parsed"]["missing_fields"])
        self.assertEqual(submitted["status"], "draft")

    # 4. malformed message (non-text, non-image event -- image events are
    # handled separately as evidence, see test_evidence_store.py)
    def test_malformed_message(self):
        event = {"from": "+12025550123", "type": "location", "location": {"latitude": 1, "longitude": 2}}
        summary, client = self._run(
            [inbox_row(event=event)],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}])
        self.assertEqual(summary["submitted"], 1)
        submitted = client.post.call_args.kwargs["json"][0]
        self.assertIsNone(submitted["payload"]["parsed"]["intent"])
        self.assertTrue(submitted["payload"]["parsed"]["errors"])
        self.assertEqual(submitted["status"], "draft")

    # 5. duplicate webhook processing must not create duplicate submissions
    def test_duplicate_processing_is_idempotent(self):
        summary, client = self._run(
            [inbox_row()],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}])
        submissions_calls = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(len(submissions_calls), 1)
        self.assertEqual(submissions_calls[0].kwargs["params"]["on_conflict"], "inbox_id")
        submissions_calls = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(len(submissions_calls), 1)
        self.assertIn("ignore-duplicates",
            submissions_calls[0].kwargs["headers"]["Prefer"])
        # A second run only ever considers rows still at status=received; once this
        # row is marked processed, a fresh scan returns nothing left to submit.
        summary2, client2 = self._run([], [], [])
        self.assertEqual(summary2, {"scanned": 0, "submitted": 0, "unmatched": 0, "skipped_non_message": 0,
            "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0,
            "reviews_confirmed": 0, "reviews_rejected": 0, "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0, "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0})
        client2.post.assert_not_called()

    # 6. message for wrong/unmapped business (no assignment, and ambiguous assignment)
    def test_unmapped_business_no_assignment(self):
        summary, client = self._run(
            [inbox_row()],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [])
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["unmatched"], 1)
        client.post.assert_not_called()

    def test_unmapped_business_ambiguous_assignment(self):
        # Same branch code in two businesses is ambiguous: no submission, no
        # guessing -- one branch-selection clarification is queued instead.
        summary, client = self._run(
            [inbox_row()],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}, {"business_id": "phone_center", "branch_id": "warri"}])
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(summary["clarifications_queued"], 1)
        submissions = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(submissions, [])
        queued = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_outbound_messages"]
        self.assertEqual(len(queued), 1)
        row = queued[0].kwargs["json"][0]
        self.assertEqual(row["message_type"], "branch_clarification")
        self.assertEqual(row["idempotency_key"], "inbox-1:branch_clarification")
        self.assertIsNone(row["business_id"])
        self.assertIsNone(row["branch_id"])
        self.assertIsNone(row["related_task_id"])
        self.assertEqual(row["recipient_employee_id"], "emp-1")
        self.assertEqual(row["provider_sender"], "+12025550123")
        self.assertIn("WARRI:", row["message_text"])
        self.assertEqual(client.patch.call_args.kwargs["json"], {"status": "unmatched"})

    # Multi-branch prefix selection (sender holds water/asaba + water/warri).
    def _multi_assignments(self):
        return [{"business_id": "water", "branch_id": "asaba"},
            {"business_id": "water", "branch_id": "warri"}]

    def _multi_identities(self):
        return [{"tenant_id": "tenant-1", "employee_id": "emp-1"}]

    def _text_event(self, body):
        return {"from": "+12025550123", "type": "text", "text": {"body": body}}

    def _clarification_posts(self, client):
        return [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_outbound_messages"]

    def _submission_posts(self, client):
        return [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]

    def test_single_assignment_colon_text_unchanged(self):
        # One assignment: the automatic branch stands and the full original
        # text (colon included) reaches extraction untouched.
        event = self._text_event("Note: Sold 50 bags at 500")
        summary, client = self._run(
            [inbox_row(event=event)],
            self._multi_identities(),
            [{"business_id": "water", "branch_id": "warri"}])
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["clarifications_queued"], 0)
        submitted = self._submission_posts(client)[0].kwargs["json"][0]
        self.assertEqual(submitted["branch_id"], "warri")
        self.assertEqual(submitted["payload"]["message_text"], "Note: Sold 50 bags at 500")
        self.assertEqual(self._clarification_posts(client), [])

    def test_multi_branch_asaba_prefix(self):
        summary, client = self._run(
            [inbox_row(event=self._text_event("ASABA: Sold 100 bags at 500"))],
            self._multi_identities(), self._multi_assignments())
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["unmatched"], 0)
        self.assertEqual(summary["clarifications_queued"], 0)
        submitted = self._submission_posts(client)[0].kwargs["json"][0]
        self.assertEqual(submitted["business_id"], "water")
        self.assertEqual(submitted["branch_id"], "asaba")
        self.assertEqual(submitted["payload"]["message_text"], "Sold 100 bags at 500")
        self.assertEqual(submitted["payload"]["parsed"]["fields"]["quantity"], 100.0)
        self.assertEqual(self._clarification_posts(client), [])

    def test_multi_branch_warri_prefix(self):
        summary, client = self._run(
            [inbox_row(event=self._text_event("WARRI: Produced 200 bags"))],
            self._multi_identities(), self._multi_assignments())
        self.assertEqual(summary["submitted"], 1)
        submitted = self._submission_posts(client)[0].kwargs["json"][0]
        self.assertEqual(submitted["branch_id"], "warri")
        self.assertEqual(submitted["payload"]["message_text"], "Produced 200 bags")
        self.assertEqual(self._clarification_posts(client), [])

    def test_prefix_matching_case_insensitive(self):
        summary, client = self._run(
            [inbox_row(event=self._text_event("aSaBa : Sold 10 bags at 100"))],
            self._multi_identities(), self._multi_assignments())
        self.assertEqual(summary["submitted"], 1)
        submitted = self._submission_posts(client)[0].kwargs["json"][0]
        self.assertEqual(submitted["branch_id"], "asaba")
        self.assertEqual(submitted["payload"]["message_text"], "Sold 10 bags at 100")

    def test_missing_prefix_queues_clarification(self):
        summary, client = self._run(
            [inbox_row(event=self._text_event("Sold 50 bags at 500"))],
            self._multi_identities(), self._multi_assignments())
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(summary["clarifications_queued"], 1)
        self.assertEqual(self._submission_posts(client), [])
        queued = self._clarification_posts(client)
        self.assertEqual(len(queued), 1)
        row = queued[0].kwargs["json"][0]
        self.assertEqual(row["message_type"], "branch_clarification")
        self.assertIn("ASABA:", row["message_text"])
        self.assertIn("WARRI:", row["message_text"])
        self.assertEqual(client.patch.call_args.kwargs["json"], {"status": "unmatched"})

    def test_invalid_prefix_queues_clarification(self):
        summary, client = self._run(
            [inbox_row(event=self._text_event("LAGOS: Sold 50 bags at 500"))],
            self._multi_identities(), self._multi_assignments())
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(summary["clarifications_queued"], 1)
        self.assertEqual(self._submission_posts(client), [])
        self.assertEqual(len(self._clarification_posts(client)), 1)

    def test_unassigned_branch_prefix_queues_clarification(self):
        # 'aba' is a real-looking branch code but not assigned to this sender.
        summary, client = self._run(
            [inbox_row(event=self._text_event("ABA: Sold 50 bags at 500"))],
            self._multi_identities(), self._multi_assignments())
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["clarifications_queued"], 1)
        self.assertEqual(self._submission_posts(client), [])

    def test_empty_prefixed_message_queues_clarification(self):
        for body in ("ASABA:", "ASABA:   "):
            summary, client = self._run(
                [inbox_row(event=self._text_event(body))],
                self._multi_identities(), self._multi_assignments())
            self.assertEqual(summary["submitted"], 0)
            self.assertEqual(summary["clarifications_queued"], 1)
            self.assertEqual(self._submission_posts(client), [])

    def test_review_commands_need_no_prefix(self):
        with patch("message_processor.review_service.handle_review_command",
                new_callable=AsyncMock) as handle:
            summary, client = self._run(
                [inbox_row(event=self._text_event("REVIEW CONFIRM YR-ABCD234EFG KEY k1"))],
                self._multi_identities(), self._multi_assignments())
        handle.assert_called_once()
        self.assertEqual(handle.call_args.args[2], "+12025550123")
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["clarifications_queued"], 0)
        self.assertEqual(self._submission_posts(client), [])
        self.assertEqual(self._clarification_posts(client), [])

    def test_retry_sends_single_clarification(self):
        rows = [inbox_row(event=self._text_event("Sold 50 bags at 500"))]
        summary, client = self._run(rows, self._multi_identities(), self._multi_assignments())
        self.assertEqual(len(self._clarification_posts(client)), 1)
        # A retry re-processes the same received row; the existing queued
        # clarification is found first, so nothing is posted again.
        summary2, client2 = self._run(
            rows, self._multi_identities(), self._multi_assignments(),
            outbound_existing=[{"id": "out-1"}])
        self.assertEqual(summary2["clarifications_queued"], 0)
        self.assertEqual(self._clarification_posts(client2), [])

    # 7. no accounting transaction created before approval: ingestion
    # performs reads, one draft submission insert, one idempotent
    # review-request queue RPC (reference issuance + reviewer
    # notification, all inside the database), and inbox status writes --
    # and nothing else. The draft stays a draft.
    def test_no_accounting_tables_touched(self):
        summary, client = self._run(
            [inbox_row()],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}])
        get_paths = {call.args[0] for call in client.get.call_args_list}
        post_paths = {call.args[0] for call in client.post.call_args_list}
        patch_paths = {call.args[0] for call in client.patch.call_args_list}
        allowed = {"/rest/v1/biz_message_inbox", "/rest/v1/biz_sender_identities", "/rest/v1/biz_assignments",
            "/rest/v1/biz_submissions"}
        self.assertTrue(get_paths.issubset(allowed))
        self.assertEqual(post_paths, {"/rest/v1/biz_submissions",
            "/rest/v1/rpc/amose_queue_review_requests"})
        self.assertEqual(patch_paths, {"/rest/v1/biz_message_inbox"})
        queue_calls = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/rpc/amose_queue_review_requests"]
        self.assertEqual(len(queue_calls), 1)
        queue_body = queue_calls[0].kwargs["json"]
        self.assertEqual(set(queue_body), {"p_submission_id", "p_request_key"})
        self.assertEqual(queue_body["p_submission_id"], SUBMISSION_UUID)
        self.assertEqual(queue_body["p_request_key"], "queuereq:inbox-1")
        for forbidden in ("tenant_id", "business_id", "branch_id",
                "employee_id", "provider_account", "recipient"):
            self.assertNotIn(forbidden, queue_body)
        submitted = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"][0].kwargs["json"][0]
        self.assertEqual(submitted["status"], "draft")
        self.assertEqual(summary["review_requests_queued"], 1)
        self.assertEqual(summary["submitted"], 1)

    def test_status_event_is_acknowledged_without_submission(self):
        event = {"id": "wamid.1", "status": "delivered", "timestamp": "123"}
        summary, client = self._run([inbox_row(event=event, kind="status")], [], [])
        self.assertEqual(summary, {"scanned": 1, "submitted": 0, "unmatched": 0, "skipped_non_message": 1,
            "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0,
            "reviews_confirmed": 0, "reviews_rejected": 0, "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0, "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0})
        client.post.assert_not_called()
        self.assertEqual(client.patch.call_args.kwargs["json"], {"status": "processed"})

    # processing failure preserves the inbox event for retry, and records retry state
    def test_processing_failure_marks_failed_and_records_retry_state(self):
        with patch("message_processor.retry_engine.record_failure", new_callable=AsyncMock) as record_failure, \
             patch("rule_engine.extract", side_effect=RuntimeError("boom")):
            summary, client = self._run(
                [inbox_row()],
                [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
                [{"business_id": "water", "branch_id": "warri"}])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["submitted"], 0)
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[1], "inbox_processing")
        self.assertEqual(record_failure.call_args.args[2], "inbox-1")
        patch_calls = [c.kwargs["json"] for c in client.patch.call_args_list]
        failed_calls = [c for c in patch_calls if c.get("status") == "failed"]
        self.assertEqual(len(failed_calls), 1)
        self.assertIn("processing_error", failed_calls[0])


class EndpointSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_requires_api_key_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.post("/internal/process-whatsapp-inbox").status_code, 503)

    def test_wrong_key_rejected(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}):
            result = self.client.post("/internal/process-whatsapp-inbox", headers={"x-yamsi-key": "wrong"})
            self.assertEqual(result.status_code, 401)

    def test_authorized_call_returns_summary(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}), \
             patch("app.process_inbox_batch", new_callable=AsyncMock,
                   return_value={"scanned": 0, "submitted": 0, "unmatched": 0, "skipped_non_message": 0}):
            result = self.client.post("/internal/process-whatsapp-inbox", headers={"x-yamsi-key": "test-only-key"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["scanned"], 0)


if __name__ == "__main__":
    unittest.main()
