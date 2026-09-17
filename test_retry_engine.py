import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import retry_engine


class RetryPolicyValidationTests(unittest.TestCase):
    def test_well_formed_policy_accepted(self):
        valid, _ = retry_engine.validate_retry_policy({"enabled": True, "max_attempts": 5, "retry_interval_minutes": 10})
        self.assertTrue(valid)

    def test_missing_enabled_rejected(self):
        valid, _ = retry_engine.validate_retry_policy({"max_attempts": 5})
        self.assertFalse(valid)

    def test_non_numeric_field_rejected(self):
        valid, _ = retry_engine.validate_retry_policy({"enabled": True, "max_attempts": "five"})
        self.assertFalse(valid)


class GetRetryPolicyTests(unittest.TestCase):
    # 18. disabled/unconfigured retry policy does not invent a schedule
    def test_no_scope_returns_none_without_querying(self):
        with patch("retry_engine.rest_get", new_callable=AsyncMock) as rget:
            policy = asyncio.run(retry_engine.get_retry_policy("tenant-1", None, None))
        self.assertIsNone(policy)
        rget.assert_not_called()

    def test_unconfigured_returns_none(self):
        with patch("retry_engine.rest_get", new_callable=AsyncMock, return_value=[]):
            policy = asyncio.run(retry_engine.get_retry_policy("tenant-1", "nughe_farms", "warri"))
        self.assertIsNone(policy)

    def test_disabled_treated_as_unconfigured(self):
        rows = [{"value": {"enabled": False, "max_attempts": 3}}]
        with patch("retry_engine.rest_get", new_callable=AsyncMock, return_value=rows):
            policy = asyncio.run(retry_engine.get_retry_policy("tenant-1", "nughe_farms", "warri"))
        self.assertIsNone(policy)

    def test_configured_enabled_policy_returned(self):
        rows = [{"value": {"enabled": True, "max_attempts": 3, "retry_interval_minutes": 15}}]
        with patch("retry_engine.rest_get", new_callable=AsyncMock, return_value=rows):
            policy = asyncio.run(retry_engine.get_retry_policy("tenant-1", "nughe_farms", "warri"))
        self.assertEqual(policy["max_attempts"], 3)


class RecordFailureTests(unittest.TestCase):
    # 17. failed inbox processing / outbound delivery / media processing receives retry state
    def test_first_failure_without_policy_stays_pending_no_schedule(self):
        state_row = {"id": "retry-1", "attempt_count": 0}
        with patch("retry_engine.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [state_row], []]) as rget, \
             patch("retry_engine.rest_post", new_callable=AsyncMock), \
             patch("retry_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(retry_engine.record_failure(
                "tenant-1", "inbox_processing", "inbox-1", RuntimeError("boom")))
        self.assertFalse(result["policy_configured"])
        body = rpatch.call_args.args[2]
        self.assertEqual(body["state"], "pending")
        self.assertIsNone(body["next_attempt_at"])
        self.assertEqual(body["attempt_count"], 1)

    def test_failure_with_policy_schedules_next_attempt(self):
        state_row = {"id": "retry-2", "attempt_count": 0}
        policy_rows = [{"value": {"enabled": True, "max_attempts": 5, "retry_interval_minutes": 10}}]
        with patch("retry_engine.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [state_row], policy_rows]) as rget, \
             patch("retry_engine.rest_post", new_callable=AsyncMock), \
             patch("retry_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(retry_engine.record_failure(
                "tenant-1", "outbound_message", "msg-1", "send failed",
                business_id="nughe_farms", branch_id="warri"))
        self.assertTrue(result["policy_configured"])
        body = rpatch.call_args.args[2]
        self.assertEqual(body["state"], "scheduled")
        self.assertIsNotNone(body["next_attempt_at"])

    def test_exhausted_attempts_go_to_dead_letter(self):
        state_row = {"id": "retry-3", "attempt_count": 4}
        policy_rows = [{"value": {"enabled": True, "max_attempts": 5, "retry_interval_minutes": 10}}]
        with patch("retry_engine.rest_get", new_callable=AsyncMock,
                    side_effect=[[state_row], policy_rows]) as rget, \
             patch("retry_engine.rest_post", new_callable=AsyncMock), \
             patch("retry_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(retry_engine.record_failure(
                "tenant-1", "media_retrieval", "ev-1", "download failed",
                business_id="nughe_farms", branch_id="warri"))
        body = rpatch.call_args.args[2]
        self.assertEqual(body["state"], "dead_letter")
        self.assertEqual(body["attempt_count"], 5)


class RecordSuccessTests(unittest.TestCase):
    def test_marks_existing_state_resolved(self):
        with patch("retry_engine.rest_get", new_callable=AsyncMock, return_value=[{"id": "retry-1"}]), \
             patch("retry_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(retry_engine.record_success("tenant-1", "outbound_message", "msg-1"))
        self.assertEqual(result, "retry-1")
        self.assertEqual(rpatch.call_args.args[2], {"state": "resolved"})

    def test_no_existing_state_is_a_safe_noop(self):
        with patch("retry_engine.rest_get", new_callable=AsyncMock, return_value=[]), \
             patch("retry_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(retry_engine.record_success("tenant-1", "outbound_message", "msg-1"))
        self.assertIsNone(result)
        rpatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
