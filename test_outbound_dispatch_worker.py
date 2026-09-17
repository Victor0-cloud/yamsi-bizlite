import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import outbound_dispatch_worker


QUEUED_ROW = {"id": "msg-1", "tenant_id": "tenant-1", "business_id": "nughe_farms", "branch_id": "warri",
    "status": "queued", "provider_sender": "+12025550123", "provider_account": "acct-1",
    "message_text": "Please send a photo."}


class ClaimTests(unittest.TestCase):
    def test_successful_claim_returns_row(self):
        response = httpx.Response(200, json=[{**QUEUED_ROW, "status": "sending"}])
        with patch("outbound_dispatch_worker.rest_patch", new_callable=AsyncMock, return_value=response):
            claimed = asyncio.run(outbound_dispatch_worker._claim("msg-1"))
        self.assertEqual(claimed["status"], "sending")

    # 14. queued outbound message claimed once -- a losing concurrent claim gets nothing
    def test_lost_claim_returns_none(self):
        response = httpx.Response(200, json=[])
        with patch("outbound_dispatch_worker.rest_patch", new_callable=AsyncMock, return_value=response):
            claimed = asyncio.run(outbound_dispatch_worker._claim("msg-1"))
        self.assertIsNone(claimed)


class DispatchPendingTests(unittest.TestCase):
    def _run(self, claim_result, dispatch_result=None, dispatch_side_effect=None):
        with patch("outbound_dispatch_worker.rest_get", new_callable=AsyncMock, return_value=[QUEUED_ROW]) as rget, \
             patch("outbound_dispatch_worker._claim", new_callable=AsyncMock, return_value=claim_result) as claim, \
             patch("outbound_dispatch_worker.rest_patch", new_callable=AsyncMock) as rpatch, \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message", new_callable=AsyncMock,
                   return_value=dispatch_result, side_effect=dispatch_side_effect) as dispatch, \
             patch("outbound_dispatch_worker.retry_engine.record_success", new_callable=AsyncMock) as record_success, \
             patch("outbound_dispatch_worker.retry_engine.record_failure", new_callable=AsyncMock) as record_failure:
            summary = asyncio.run(outbound_dispatch_worker.dispatch_pending())
        return summary, dict(rget=rget, claim=claim, rpatch=rpatch, dispatch=dispatch,
            record_success=record_success, record_failure=record_failure)

    # 7 (again, via the worker this time). outbound WhatsApp API mocked successfully
    def test_successful_dispatch_records_success(self):
        claimed = {**QUEUED_ROW, "status": "sending"}
        summary, mocks = self._run(claimed, dispatch_result={"status": "sent", "provider_message_id": "wamid.1"})
        self.assertEqual(summary, {"scanned": 1, "sent": 1, "failed": 0, "claim_conflicts": 0})
        mocks["record_success"].assert_called_once_with("tenant-1", "outbound_message", "msg-1")
        mocks["record_failure"].assert_not_called()

    # 15. duplicate dispatch does not duplicate send -- claim conflict short-circuits
    def test_claim_conflict_is_not_dispatched(self):
        summary, mocks = self._run(None)
        self.assertEqual(summary, {"scanned": 1, "sent": 0, "failed": 0, "claim_conflicts": 1})
        mocks["dispatch"].assert_not_called()

    # 16. failed outbound delivery receives retry state
    def test_failed_dispatch_records_retry_state(self):
        claimed = {**QUEUED_ROW, "status": "sending"}
        summary, mocks = self._run(claimed, dispatch_result={"status": "failed", "failure_reason": "bad request"})
        self.assertEqual(summary["failed"], 1)
        mocks["record_failure"].assert_called_once()
        self.assertEqual(mocks["record_failure"].call_args.args[1], "outbound_message")

    def test_unexpected_exception_marks_failed_and_records_retry_state(self):
        claimed = {**QUEUED_ROW, "status": "sending"}
        summary, mocks = self._run(claimed, dispatch_side_effect=RuntimeError("boom"))
        self.assertEqual(summary["failed"], 1)
        mocks["record_failure"].assert_called_once()
        patched_body = mocks["rpatch"].call_args.args[2]
        self.assertEqual(patched_body["status"], "failed")


if __name__ == "__main__":
    unittest.main()
