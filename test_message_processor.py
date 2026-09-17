import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from app import app
from message_processor import parse_message, process_inbox_batch

MESSAGE_EVENT = {"from": "+12025550123", "type": "text", "text": {"body": "Sold 50 bags at 500"}}


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


class ProcessInboxTests(unittest.TestCase):
    def _client(self, identities, assignments, post_status=201, patch_status=204):
        client = AsyncMock()
        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_message_inbox":
                return httpx.Response(200, json=self._inbox_rows)
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=identities)
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=assignments)
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": "submission-1"}])
            raise AssertionError("unexpected GET " + path)
        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(return_value=httpx.Response(post_status))
        client.patch = AsyncMock(return_value=httpx.Response(patch_status))
        return client

    def _run(self, inbox_rows, identities, assignments, post_status=201, patch_status=204):
        self._inbox_rows = inbox_rows
        client = self._client(identities, assignments, post_status, patch_status)
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
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0})
        submitted = client.post.call_args.kwargs["json"][0]
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
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0})
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
        submitted = client.post.call_args.kwargs["json"][0]
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
        self.assertEqual(client.post.call_args.kwargs["params"]["on_conflict"], "inbox_id")
        self.assertIn("ignore-duplicates", client.post.call_args.kwargs["headers"]["Prefer"])
        # A second run only ever considers rows still at status=received; once this
        # row is marked processed, a fresh scan returns nothing left to submit.
        summary2, client2 = self._run([], [], [])
        self.assertEqual(summary2, {"scanned": 0, "submitted": 0, "unmatched": 0, "skipped_non_message": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0})
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
        summary, client = self._run(
            [inbox_row()],
            [{"tenant_id": "tenant-1", "employee_id": "emp-1"}],
            [{"business_id": "water", "branch_id": "warri"}, {"business_id": "phone_center", "branch_id": "warri"}])
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["unmatched"], 1)
        client.post.assert_not_called()

    # 7. no accounting transaction created before approval
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
        self.assertEqual(post_paths, {"/rest/v1/biz_submissions"})
        self.assertEqual(patch_paths, {"/rest/v1/biz_message_inbox"})
        submitted = client.post.call_args.kwargs["json"][0]
        self.assertEqual(submitted["status"], "draft")

    def test_status_event_is_acknowledged_without_submission(self):
        event = {"id": "wamid.1", "status": "delivered", "timestamp": "123"}
        summary, client = self._run([inbox_row(event=event, kind="status")], [], [])
        self.assertEqual(summary, {"scanned": 1, "submitted": 0, "unmatched": 0, "skipped_non_message": 1,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0, "failed": 0})
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
