import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import brain
import owner_query

AMOS = {"id": "amos-emp", "display_name": "Amos"}
PHILLIP = {"id": "phillip-emp", "display_name": "Phillip"}
NUGHE_WARRI = {"business_id": "nughe_farms", "business_name": "Nughe Farms", "branch_id": "warri", "branch_name": "Warri"}
AMOSE_ASABA = {"business_id": "amose_table_water", "business_name": "AMOSE Table Water",
    "branch_id": "asaba", "branch_name": "Asaba"}
KNOWN_EMPLOYEES = [AMOS, PHILLIP]
KNOWN_BUSINESSES = [NUGHE_WARRI, AMOSE_ASABA]


class InterpretOwnerQuestionTests(unittest.TestCase):
    # 1. owner natural language -> valid approved intent
    def test_what_did_amos_report_today(self):
        result = brain.interpret_owner_question("What did Amos report today?",
            known_employees=KNOWN_EMPLOYEES, known_businesses=KNOWN_BUSINESSES)
        self.assertFalse(result["clarification_needed"])
        intent = result["intent"]
        self.assertEqual(intent["query_type"], "submission_status")
        self.assertEqual(intent["employee_id"], "amos-emp")
        self.assertIsNotNone(intent["date"])

    def test_crates_produced_today_business_named(self):
        result = brain.interpret_owner_question("How many crates did Nughe Farms produce today?",
            known_businesses=KNOWN_BUSINESSES)
        intent = result["intent"]
        self.assertEqual(intent["query_type"], "crate_count")
        self.assertEqual(intent["business_id"], "nughe_farms")
        self.assertEqual(intent["branch_id"], "warri")

    def test_mortality_this_week_question(self):
        result = brain.interpret_owner_question("Was there any mortality this week?")
        self.assertEqual(result["intent"]["query_type"], "mortality_this_week")

    def test_missing_evidence_question(self):
        result = brain.interpret_owner_question("Which reports are missing pictures?")
        self.assertEqual(result["intent"]["query_type"], "missing_evidence")

    def test_outstanding_tasks_question(self):
        result = brain.interpret_owner_question("What tasks are still outstanding?")
        self.assertEqual(result["intent"]["query_type"], "outstanding_tasks")

    def test_did_amos_send_todays_report(self):
        result = brain.interpret_owner_question("Did Amos send today's report?", known_employees=KNOWN_EMPLOYEES)
        self.assertEqual(result["intent"]["query_type"], "submission_status")
        self.assertEqual(result["intent"]["employee_id"], "amos-emp")

    # unrecognized intent -> no fabrication
    def test_unrecognized_question(self):
        result = brain.interpret_owner_question("What's the capital of France?")
        self.assertFalse(result["clarification_needed"])
        self.assertIsNone(result["intent"]["query_type"])

    # 2. ambiguous owner question -> clarification rather than fabrication
    def test_ambiguous_business_name_asks_for_clarification(self):
        two_branches_same_business = [NUGHE_WARRI, {**NUGHE_WARRI, "branch_id": "asaba", "branch_name": "Asaba"}]
        result = brain.interpret_owner_question("What did Nughe Farms report today?",
            known_businesses=two_branches_same_business)
        self.assertTrue(result["clarification_needed"])
        self.assertIsNone(result["intent"])

    def test_ambiguous_employee_name_asks_for_clarification(self):
        # two distinct employee ids sharing the exact same display name
        collision = [AMOS, {"id": "amos-2", "display_name": "Amos"}]
        result = brain.interpret_owner_question("Did Amos submit today's report?", known_employees=collision)
        self.assertTrue(result["clarification_needed"])


class AnswerOwnerQuestionTests(unittest.TestCase):
    # 4. known-data question -> exact database result
    def test_known_data_returns_exact_value(self):
        rows = [{"payload": {"parsed": {"fields": {"crates": 26}}}}]
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=rows):
            result = asyncio.run(brain.answer_owner_question("tenant-1",
                "How many crates did Nughe Farms produce today?",
                is_owner=True, known_businesses=KNOWN_BUSINESSES))
        self.assertTrue(result["recorded"])
        self.assertEqual(result["data"]["crates"], 26)

    # 3. no-data question -> Not recorded
    def test_no_data_says_not_recorded(self):
        with patch("owner_query.rest_get", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(brain.answer_owner_question("tenant-1",
                "How many crates did Nughe Farms produce today?",
                is_owner=True, known_businesses=KNOWN_BUSINESSES))
        self.assertFalse(result["recorded"])
        self.assertEqual(result["answer"], "Not recorded.")

    def test_clarification_needed_never_queries_database(self):
        collision = [AMOS, {"id": "amos-2", "display_name": "Amos"}]
        with patch("owner_query.rest_get", new_callable=AsyncMock) as rget:
            result = asyncio.run(brain.answer_owner_question("tenant-1", "Did Amos submit today's report?",
                is_owner=True, known_employees=collision))
        rget.assert_not_called()
        self.assertTrue(result["clarification_needed"])
        self.assertFalse(result["recorded"])

    # 8. AI cannot submit arbitrary SQL / 9. AI cannot perform arbitrary DB write
    def test_malformed_interpretation_never_reaches_database(self):
        # Simulates what a future LLM's raw output might look like before
        # validation -- brain.py's interpret step would never itself
        # fabricate this, but answer_from_intent must still refuse it.
        with patch("owner_query.rest_get", new_callable=AsyncMock) as rget:
            result = asyncio.run(owner_query.answer_from_intent(
                "tenant-1", {"query_type": "DROP TABLE biz_submissions;"}, is_owner=True))
        rget.assert_not_called()
        self.assertFalse(result["recorded"])


class LoadReferenceDataTests(unittest.TestCase):
    def test_joins_branches_to_business_names(self):
        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_employees":
                return [{"id": "amos-emp", "display_name": "Amos"}]
            if path == "/rest/v1/biz_businesses":
                return [{"id": "nughe_farms", "name": "Nughe Farms"}]
            if path == "/rest/v1/biz_branches":
                return [{"business_id": "nughe_farms", "id": "warri", "name": "Warri"}]
            raise AssertionError("unexpected GET " + path)
        with patch("brain.rest_get", new_callable=AsyncMock, side_effect=fake_get):
            employees, businesses = asyncio.run(brain.load_reference_data("tenant-1"))
        self.assertEqual(employees, [{"id": "amos-emp", "display_name": "Amos"}])
        self.assertEqual(businesses, [{"business_id": "nughe_farms", "business_name": "Nughe Farms",
            "branch_id": "warri", "branch_name": "Warri"}])


class OwnerQueryEndpointNaturalLanguageTests(unittest.TestCase):
    def setUp(self):
        import os
        from fastapi.testclient import TestClient
        from app import app
        self.TestClient = TestClient
        self.app = app
        self.env = patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)

    def test_natural_language_question_resolves_entities_and_answers(self):
        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_employees":
                return []
            if path == "/rest/v1/biz_businesses":
                return [{"id": "nughe_farms", "name": "Nughe Farms"}]
            if path == "/rest/v1/biz_branches":
                return [{"business_id": "nughe_farms", "id": "warri", "name": "Warri"}]
            if path == "/rest/v1/biz_submissions":
                return [{"payload": {"parsed": {"fields": {"crates": 26}}}}]
            raise AssertionError("unexpected GET " + path)
        with patch("brain.rest_get", new_callable=AsyncMock, side_effect=fake_get), \
             patch("owner_query.rest_get", new_callable=AsyncMock, side_effect=fake_get):
            result = self.client.post("/internal/owner-query", headers={"x-yamsi-key": "test-only-key"},
                json={"question": "How many crates did Nughe Farms produce today?"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["recorded"])
        self.assertEqual(result.json()["data"]["crates"], 26)


if __name__ == "__main__":
    unittest.main()
