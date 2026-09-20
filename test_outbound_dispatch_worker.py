import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import outbound_dispatch_worker


QUEUED_ROW = {"id": "msg-1", "tenant_id": "tenant-1", "business_id": "nughe_farms", "branch_id": "warri",
    "status": "queued", "provider_sender": "+12025550123", "provider_account": "acct-1",
    "message_text": "Please send a photo."}


class ParseLimitTests(unittest.TestCase):
    def test_missing_and_garbage_fall_back_to_default(self):
        self.assertEqual(outbound_dispatch_worker.parse_limit(None),
            outbound_dispatch_worker.BATCH_LIMIT)
        self.assertEqual(outbound_dispatch_worker.parse_limit("many"),
            outbound_dispatch_worker.BATCH_LIMIT)

    def test_bounds_are_clamped(self):
        self.assertEqual(outbound_dispatch_worker.parse_limit(0), 1)
        self.assertEqual(outbound_dispatch_worker.parse_limit(-5), 1)
        self.assertEqual(outbound_dispatch_worker.parse_limit(500), 100)

    def test_valid_values_pass_through(self):
        self.assertEqual(outbound_dispatch_worker.parse_limit(5), 5)
        self.assertEqual(outbound_dispatch_worker.parse_limit("7"), 7)


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

    # The send path must use the row's own provider_account (the business
    # number snapshotted at queue time) -- never a re-derived value.
    def test_send_uses_row_provider_account(self):
        claimed = {**QUEUED_ROW, "status": "sending", "provider_account": "1350537361474836"}
        summary, mocks = self._run(claimed,
            dispatch_result={"status": "sent", "provider_message_id": "wamid.1"})
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(mocks["dispatch"].call_args.args[1], "1350537361474836")

    # Two concurrent passes over the same queued rows send exactly once:
    # the atomic claim lets one pass through and the loser sees a conflict.
    def test_concurrent_passes_send_once(self):
        row = dict(QUEUED_ROW)
        state = {"taken": False}
        sends = []
        async def fake_get(path, params=None):
            return [row]
        async def fake_patch(path, params, body, prefer=None):
            if body.get("status") == "sending" and params.get("status") == "eq.queued":
                if state["taken"]:
                    return httpx.Response(200, json=[])
                state["taken"] = True
                return httpx.Response(200, json=[{**row, "status": "sending"}])
            return httpx.Response(200, json=[{**row, **body}])
        async def fake_send(claimed, account):
            sends.append(claimed["id"])
            return {"status": "sent", "provider_message_id": "wamid.1"}
        async def main():
            return await asyncio.gather(
                outbound_dispatch_worker.dispatch_pending(),
                outbound_dispatch_worker.dispatch_pending())
        with patch("outbound_dispatch_worker.rest_get", new_callable=AsyncMock, side_effect=fake_get), \
             patch("outbound_dispatch_worker.rest_patch", new_callable=AsyncMock, side_effect=fake_patch), \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message",
                   new_callable=AsyncMock, side_effect=fake_send), \
             patch("outbound_dispatch_worker.retry_engine.record_success", new_callable=AsyncMock), \
             patch("outbound_dispatch_worker.retry_engine.record_failure", new_callable=AsyncMock):
            summaries = asyncio.run(main())
        self.assertEqual(sends, ["msg-1"])
        self.assertEqual(sorted(s["sent"] for s in summaries), [0, 1])
        self.assertEqual(sorted(s["claim_conflicts"] for s in summaries), [0, 1])

    # A branch_clarification row (null scope by design) dispatches exactly
    # like any other queued row: recipient and text come from the row.
    def test_clarification_row_dispatches(self):
        row = {"id": "msg-9", "tenant_id": "tenant-1", "business_id": None, "branch_id": None,
            "status": "queued", "provider_sender": "+12025550123", "provider_account": "acct-9",
            "message_text": "Please begin your message with ASABA: or WARRI: so YAMSI knows which branch to use."}
        claimed = {**row, "status": "sending"}
        summary, mocks = self._run(claimed,
            dispatch_result={"status": "sent", "provider_message_id": "wamid.9"})
        self.assertEqual(summary, {"scanned": 1, "sent": 1, "failed": 0, "claim_conflicts": 0})
        self.assertEqual(mocks["dispatch"].call_args.args[0]["message_text"], row["message_text"])
        self.assertEqual(mocks["dispatch"].call_args.args[1], "acct-9")


if __name__ == "__main__":
    unittest.main()
