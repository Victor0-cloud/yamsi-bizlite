import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app import app
import owner_query


class ClassifyTests(unittest.TestCase):
    def test_crate_question(self):
        self.assertEqual(owner_query.classify("How many crates were produced today?"), "crate_count")

    def test_submission_question(self):
        self.assertEqual(owner_query.classify("Did Amos submit today's report?"), "submission_status")

    def test_mortality_question(self):
        self.assertEqual(owner_query.classify("Was there mortality this week?"), "mortality_this_week")

    def test_missing_evidence_question(self):
        self.assertEqual(owner_query.classify("Which reports are missing proof?"), "missing_evidence")

    def test_outstanding_tasks_question(self):
        self.assertEqual(owner_query.classify("What tasks are outstanding?"), "outstanding_tasks")

    def test_unrecognized_question(self):
        self.assertIsNone(owner_query.classify("What's the weather like?"))

    # word-order independence -- item 2 examples
    def test_send_today_report_word_order(self):
        self.assertEqual(owner_query.classify("Did Amos send today's report?"), "submission_status")

    def test_missing_pictures_phrasing(self):
        self.assertEqual(owner_query.classify("Which reports are missing pictures?"), "missing_evidence")

    def test_still_outstanding_phrasing(self):
        self.assertEqual(owner_query.classify("What tasks are still outstanding?"), "outstanding_tasks")


class NewIntentTypesTests(unittest.TestCase):
    def test_employee_report_status_known(self):
        rows = [{"id": "sub-1", "kind": "poultry_daily_report"}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(owner_query.employee_report_status("tenant-1", "amos-emp"))
        self.assertTrue(result["recorded"])

    def test_employee_report_status_not_recorded(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(owner_query.employee_report_status("tenant-1", "phillip-emp"))
        self.assertFalse(result["recorded"])
        self.assertEqual(result["answer"], owner_query.NOT_RECORDED)

    def test_business_summary_aggregates_all_four(self):
        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_submissions":
                return [{"payload": {"parsed": {"fields": {"crates": 26}}}}]
            if path == "/rest/v1/biz_mortality_incidents":
                return [{"mortality_count": 2, "created_at": "x", "suspected_cause": None}]
            if path == "/rest/v1/biz_evidence":
                return []
            if path == "/rest/v1/biz_tasks":
                return []
            raise AssertionError("unexpected GET " + path)
        with patch("owner_query.rest_get", new_callable=AsyncMock, side_effect=fake_get):
            result = asyncio.run(owner_query.business_summary("tenant-1", "nughe_farms", "warri"))
        self.assertTrue(result["recorded"])
        self.assertEqual(result["data"]["crates"]["data"]["crates"], 26)
        self.assertEqual(result["data"]["missing_evidence"]["recorded"], False)

    def test_production_summary_alias_routes_to_crate_count(self):
        rows = [{"payload": {"parsed": {"fields": {"crates": 10}}}}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(owner_query.dispatch("tenant-1", "production_summary",
                business_id="nughe_farms", branch_id="warri"))
        self.assertEqual(result["data"]["crates"], 10)


class NotRecordedTests(unittest.TestCase):
    # 12. missing business data: owner query says not recorded
    def test_crate_count_not_recorded(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(owner_query.crate_count("tenant-1", "nughe_farms", "warri", "2026-09-16"))
        self.assertFalse(result["recorded"])
        self.assertEqual(result["answer"], owner_query.NOT_RECORDED)

    def test_mortality_not_recorded(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(owner_query.mortality_this_week("tenant-1", "nughe_farms", "warri"))
        self.assertFalse(result["recorded"])
        self.assertEqual(result["answer"], owner_query.NOT_RECORDED)

    def test_submission_status_not_recorded(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(owner_query.submission_status("tenant-1", "nughe_farms", "warri", "amos-emp", "2026-09-16"))
        self.assertFalse(result["recorded"])
        self.assertEqual(result["answer"], owner_query.NOT_RECORDED)


class KnownDataTests(unittest.TestCase):
    # 13. known recorded data: owner query returns exact stored value
    def test_crate_count_returns_exact_sum(self):
        rows = [{"payload": {"parsed": {"fields": {"crates": 26}}}}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(owner_query.crate_count("tenant-1", "nughe_farms", "warri", "2026-09-16"))
        self.assertTrue(result["recorded"])
        self.assertEqual(result["data"]["crates"], 26)

    def test_mortality_returns_exact_total(self):
        rows = [{"mortality_count": 3, "created_at": "2026-09-15T00:00:00Z", "suspected_cause": None}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(owner_query.mortality_this_week("tenant-1", "nughe_farms", "warri"))
        self.assertTrue(result["recorded"])
        self.assertIn("3", result["answer"])

    def test_submission_status_recorded(self):
        rows = [{"id": "sub-1", "kind": "poultry_daily_report", "created_at": "2026-09-16T09:00:00Z"}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(owner_query.submission_status("tenant-1", "nughe_farms", "warri", "amos-emp", "2026-09-16"))
        self.assertTrue(result["recorded"])
        self.assertEqual(result["data"], rows)


class AnswerDispatchAuthorizationTests(unittest.TestCase):
    def test_owner_not_restricted(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(owner_query.answer("tenant-1", "How many crates today?", is_owner=True,
                business_id="nughe_farms", branch_id="warri"))
        self.assertEqual(result["query_type"], "crate_count")
        self.assertFalse(result["recorded"])

    # 16. unauthorized employee cannot cross business/branch boundaries
    def test_staff_blocked_outside_own_assignment(self):
        result = asyncio.run(owner_query.answer("tenant-1", "How many crates today?", is_owner=False,
            caller_assignments=[("amose_table_water", "asaba")], business_id="nughe_farms", branch_id="warri"))
        self.assertIn("not authorized", result["answer"])
        self.assertFalse(result["recorded"])

    def test_staff_allowed_within_own_assignment(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(owner_query.answer("tenant-1", "How many crates today?", is_owner=False,
                caller_assignments=[("nughe_farms", "warri")], business_id="nughe_farms", branch_id="warri"))
        self.assertEqual(result["query_type"], "crate_count")

    def test_unrecognized_question_never_hallucinates(self):
        result = asyncio.run(owner_query.answer("tenant-1", "What's the capital of France?", is_owner=True))
        self.assertIsNone(result["query_type"])
        self.assertFalse(result["recorded"])


class IntentInterfaceTests(unittest.TestCase):
    # 8. clean validated-intent interface for a future NLU/LLM layer
    def test_valid_intent_passes(self):
        valid, reason = owner_query.validate_intent(
            {"query_type": "crate_count", "business_id": "nughe_farms", "branch_id": "warri"})
        self.assertTrue(valid)

    def test_unrecognized_query_type_rejected(self):
        valid, reason = owner_query.validate_intent({"query_type": "drop_all_tables"})
        self.assertFalse(valid)

    def test_missing_required_field_rejected(self):
        valid, reason = owner_query.validate_intent({"query_type": "crate_count", "business_id": "nughe_farms"})
        self.assertFalse(valid)
        self.assertIn("branch_id", reason)

    def test_answer_from_intent_known_data(self):
        rows = [{"payload": {"parsed": {"fields": {"crates": 26}}}}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(owner_query.answer_from_intent("tenant-1",
                {"query_type": "crate_count", "business_id": "nughe_farms", "branch_id": "warri"}, is_owner=True))
        self.assertTrue(result["recorded"])
        self.assertEqual(result["data"]["crates"], 26)

    def test_answer_from_intent_invalid_never_queries(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock) as rget:
            result = asyncio.run(owner_query.answer_from_intent("tenant-1",
                {"query_type": "not_a_real_type"}, is_owner=True))
        rget.assert_not_called()
        self.assertFalse(result["recorded"])

    def test_answer_from_intent_respects_authorization_boundary(self):
        result = asyncio.run(owner_query.answer_from_intent("tenant-1",
            {"query_type": "crate_count", "business_id": "nughe_farms", "branch_id": "warri"},
            is_owner=False, caller_assignments=[("amose_table_water", "asaba")]))
        self.assertFalse(result["recorded"])
        self.assertIn("not authorized", result["answer"])


class OwnerQueryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.client.post("/internal/owner-query", json={"question": "How many crates today?"})
            self.assertEqual(result.status_code, 503)

    def test_known_data_returns_exact_value(self):
        rows = [{"payload": {"parsed": {"fields": {"crates": 26}}}}]
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}), \
             patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = self.client.post("/internal/owner-query", headers={"x-yamsi-key": "test-only-key"},
                json={"question": "How many crates today?", "business_id": "nughe_farms", "branch_id": "warri"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["recorded"])
        self.assertEqual(result.json()["data"]["crates"], 26)

    def test_missing_data_says_not_recorded(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}), \
             patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = self.client.post("/internal/owner-query", headers={"x-yamsi-key": "test-only-key"},
                json={"question": "How many crates today?", "business_id": "nughe_farms", "branch_id": "warri"})
        self.assertFalse(result.json()["recorded"])
        self.assertEqual(result.json()["answer"], owner_query.NOT_RECORDED)


if __name__ == "__main__":
    unittest.main()
