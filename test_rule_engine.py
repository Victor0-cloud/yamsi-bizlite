import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import rule_engine


class ParsePoultryReportTests(unittest.TestCase):
    def test_full_report(self):
        result = rule_engine.parse_poultry_report("300 eggs, 26 crates, fed 4.5 bags feed, 0 died")
        self.assertEqual(result["fields"]["eggs_produced"], 300.0)
        self.assertEqual(result["fields"]["crates"], 26.0)
        self.assertEqual(result["fields"]["feed_used"], 4.5)
        self.assertEqual(result["fields"]["mortality_count"], 0)
        self.assertEqual(result["provenance"]["crates"], "staff_reported")

    def test_crates_only_missing_others(self):
        result = rule_engine.parse_poultry_report("26 crates today")
        self.assertEqual(result["fields"]["crates"], 26.0)
        self.assertIn("eggs_produced", result["missing_fields"])
        self.assertIn("mortality_count", result["missing_fields"])
        self.assertEqual(result["errors"], [])

    def test_mortality_with_unknown_cause(self):
        result = rule_engine.parse_poultry_report("3 birds died today")
        self.assertEqual(result["fields"]["mortality_count"], 3)
        self.assertNotIn("suspected_cause", result["fields"])

    def test_mortality_with_stated_cause(self):
        result = rule_engine.parse_poultry_report("3 birds died, cause: heat stress")
        self.assertEqual(result["fields"]["suspected_cause"], "heat stress")
        self.assertEqual(result["provenance"]["suspected_cause"], "staff_reported")

    def test_unrecognized_text(self):
        result = rule_engine.parse_poultry_report("good morning")
        self.assertTrue(result["errors"])

    def test_empty_text(self):
        result = rule_engine.parse_poultry_report("")
        self.assertEqual(result["errors"], ["Empty or missing message text"])

    # explicit examples from Stage 007C item 3
    def test_crates_bags_no_mortality(self):
        result = rule_engine.parse_poultry_report("26 crates today, used 5 bags feed, no mortality.")
        self.assertEqual(result["fields"]["crates"], 26.0)
        self.assertEqual(result["fields"]["feed_used"], 5.0)
        self.assertEqual(result["fields"]["mortality_count"], 0)
        self.assertEqual(result["errors"], [])

    def test_crates_and_mortality_count(self):
        result = rule_engine.parse_poultry_report("24 crates. 3 birds died.")
        self.assertEqual(result["fields"]["crates"], 24.0)
        self.assertEqual(result["fields"]["mortality_count"], 3)

    def test_word_number_feed_with_half(self):
        result = rule_engine.parse_poultry_report("We used four and half bags feed today.")
        self.assertEqual(result["fields"]["feed_used"], 4.5)
        self.assertEqual(result["provenance"]["feed_used"], "staff_reported")

    def test_missing_value_stays_missing_not_zero(self):
        result = rule_engine.parse_poultry_report("26 crates today.")
        self.assertNotIn("mortality_count", result["fields"])
        self.assertIn("mortality_count", result["missing_fields"])


class ExtractDispatchTests(unittest.TestCase):
    def test_nughe_farms_warri_uses_poultry_parser(self):
        extraction = rule_engine.extract("nughe_farms", "warri", "26 crates today", received_at="2026-09-16T10:00:00Z")
        self.assertEqual(extraction["kind"], "poultry_daily_report")
        self.assertEqual(extraction["fields"]["crates"], 26.0)
        self.assertEqual(extraction["fields"]["reporting_date"], "2026-09-16")
        self.assertEqual(extraction["provenance"]["reporting_date"], "system_derived")

    def test_other_business_uses_sale_parser_unchanged(self):
        extraction = rule_engine.extract("amose_table_water", "asaba", "Sold 50 bags at 500")
        self.assertEqual(extraction["kind"], "sale")
        self.assertEqual(extraction["fields"]["quantity"], 50.0)

    def test_amose_table_water_never_gets_poultry_rules(self):
        # A message with poultry-shaped vocabulary sent to a different
        # business must not be routed through the poultry parser.
        extraction = rule_engine.extract("amose_table_water", "warri", "26 crates today")
        self.assertNotEqual(extraction["kind"], "poultry_daily_report")


class ApplyRulesTests(unittest.TestCase):
    def _run(self, business_id, branch_id, fields, **patches):
        extraction = {"kind": "poultry_daily_report", "fields": fields}
        with patch("rule_engine.evidence_store.create_requirement", new_callable=AsyncMock,
                    return_value={"id": "evidence-1"}) as create_req, \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock,
                    return_value={"id": "task-1"}) as create_task, \
             patch("rule_engine.rest_get", new_callable=AsyncMock, return_value=[]) as rget, \
             patch("rule_engine.rest_post", new_callable=AsyncMock) as rpost, \
             patch("rule_engine.rest_patch", new_callable=AsyncMock) as rpatch, \
             patch("rule_engine._ensure_reminder", new_callable=AsyncMock,
                    return_value={"reminder_id": "rem-1", "policy_configured": False}) as ensure_reminder, \
             patch("rule_engine.notifier.queue_message", new_callable=AsyncMock,
                    return_value={"id": "msg-1", "status": "skipped_no_identity"}) as queue_message:
            if "resolve_phillip" in patches:
                rget.side_effect = patches["resolve_phillip"]
            result = asyncio.run(rule_engine.apply_rules(
                "tenant-1", business_id, branch_id, "amos-emp", "submission-1", extraction, inbox_id="inbox-1"))
            return result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message

    # scoping: rules never fire outside nughe_farms/warri
    def test_no_rules_outside_scope(self):
        result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message = self._run(
            "amose_table_water", "asaba", {"crates": 26})
        self.assertEqual(result, {"applied": False})
        create_req.assert_not_called()
        create_task.assert_not_called()
        queue_message.assert_not_called()

    # 3. crate report without image -> evidence required, follow-up task created,
    # exactly one evidence-request message queued
    def test_crate_report_creates_evidence_and_task(self):
        result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message = self._run(
            "nughe_farms", "warri", {"crates": 26})
        create_req.assert_called_once_with("tenant-1", "nughe_farms", "warri", "submission-1", employee_id="amos-emp")
        create_task.assert_called_once()
        self.assertEqual(create_task.call_args.args[3], "evidence_request_crate")
        self.assertEqual(result["evidence_id"], "evidence-1")
        ensure_reminder.assert_called_once_with("tenant-1", "nughe_farms", "warri", "evidence-1")
        queue_message.assert_called_once()
        self.assertEqual(queue_message.call_args.args[5], "evidence_request_crate")
        self.assertEqual(len(result["queued_messages"]), 1)

    # 5. mortality_count = 0 -> no mortality requirement, no incident, no Phillip task
    def test_zero_mortality_no_requirement(self):
        result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message = self._run(
            "nughe_farms", "warri", {"mortality_count": 0})
        create_req.assert_not_called()
        rpost.assert_not_called()
        queue_message.assert_not_called()
        self.assertIsNone(result["mortality_incident_id"])
        self.assertIsNone(result["phillip_task_id"])

    # 6 & 7. mortality_count > 0 -> incident created + evidence required + Phillip task,
    # exactly one evidence-request message queued to the reporter
    def test_mortality_creates_incident_evidence_and_phillip_task(self):
        async def resolve_phillip(path, params):
            if path == "/rest/v1/biz_mortality_incidents":
                return []
            if path == "/rest/v1/biz_employees":
                self.assertEqual(params["display_name"], "eq.Phillip")
                return [{"id": "phillip-emp"}]
            return []
        result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message = self._run(
            "nughe_farms", "warri", {"mortality_count": 3}, resolve_phillip=resolve_phillip)
        rpost.assert_called_once()
        posted_row = rpost.call_args.args[1][0]
        self.assertEqual(posted_row["mortality_count"], 3)
        create_req.assert_called_once()
        self.assertEqual(create_task.call_count, 2)
        task_types = [call.args[3] for call in create_task.call_args_list]
        self.assertIn("evidence_request_mortality", task_types)
        self.assertIn("bird_care_followup", task_types)
        bird_care_call = next(call for call in create_task.call_args_list if call.args[3] == "bird_care_followup")
        self.assertEqual(bird_care_call.kwargs["assigned_employee_id"], "phillip-emp")
        # exactly one message queued to the reporter, one to Phillip
        self.assertEqual(queue_message.call_count, 2)
        message_types = [call.args[5] for call in queue_message.call_args_list]
        self.assertIn("evidence_request_mortality", message_types)
        self.assertIn("bird_care_followup", message_types)

    # 8. unknown cause stored as NULL, never fabricated
    def test_mortality_unknown_cause_is_null(self):
        async def resolve_phillip(path, params):
            if path == "/rest/v1/biz_employees":
                return [{"id": "phillip-emp"}]
            return []
        result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message = self._run(
            "nughe_farms", "warri", {"mortality_count": 2}, resolve_phillip=resolve_phillip)
        posted_row = rpost.call_args.args[1][0]
        self.assertIsNone(posted_row["suspected_cause"])
        self.assertIsNone(posted_row["cause_source"])

    # 9. duplicate processing: re-applying rules for a submission that already
    # has a mortality incident must not create a second one.
    def test_duplicate_mortality_incident_prevented(self):
        async def already_exists(path, params):
            if path == "/rest/v1/biz_mortality_incidents":
                return [{"id": "existing-incident"}]
            if path == "/rest/v1/biz_employees":
                return [{"id": "phillip-emp"}]
            return []
        result, create_req, create_task, rget, rpost, rpatch, ensure_reminder, queue_message = self._run(
            "nughe_farms", "warri", {"mortality_count": 1}, resolve_phillip=already_exists)
        rpost.assert_not_called()
        self.assertEqual(result["mortality_incident_id"], "existing-incident")


class ReminderPolicyTests(unittest.TestCase):
    # 16. no configured reminder policy means no invented reminder schedule
    def test_no_configured_policy_returns_none(self):
        with patch("rule_engine.rest_get", new_callable=AsyncMock, return_value=[]):
            policy = asyncio.run(rule_engine.get_reminder_policy("tenant-1", "nughe_farms", "warri"))
        self.assertIsNone(policy)

    def test_ensure_reminder_without_policy_creates_row_with_no_invented_schedule(self):
        with patch("rule_engine.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [], [{"id": "rem-1", "max_attempts": None, "next_due_at": None}]]) as rget, \
             patch("rule_engine.rest_post", new_callable=AsyncMock) as rpost:
            result = asyncio.run(rule_engine._ensure_reminder("tenant-1", "nughe_farms", "warri", "ev-1"))
        self.assertFalse(result["policy_configured"])
        posted_row = rpost.call_args.args[1][0]
        self.assertNotIn("max_attempts", posted_row)
        self.assertNotIn("next_due_at", posted_row)

    def test_ensure_reminder_with_configured_policy_uses_it(self):
        policy_row = [{"value": {"enabled": True, "max_reminders": 3,
            "first_reminder_delay_minutes": 120, "escalation_hours": 24}}]
        with patch("rule_engine.rest_get", new_callable=AsyncMock,
                    side_effect=[[], policy_row, [{"id": "rem-2"}]]) as rget, \
             patch("rule_engine.rest_post", new_callable=AsyncMock) as rpost:
            result = asyncio.run(rule_engine._ensure_reminder("tenant-1", "nughe_farms", "warri", "ev-2"))
        self.assertTrue(result["policy_configured"])
        posted_row = rpost.call_args.args[1][0]
        self.assertEqual(posted_row["max_attempts"], 3)
        self.assertIn("next_due_at", posted_row)

    def test_disabled_policy_treated_as_unconfigured(self):
        policy_row = [{"value": {"enabled": False, "max_reminders": 3, "first_reminder_delay_minutes": 120}}]
        with patch("rule_engine.rest_get", new_callable=AsyncMock, return_value=policy_row):
            policy = asyncio.run(rule_engine.get_reminder_policy("tenant-1", "nughe_farms", "warri"))
        self.assertIsNone(policy)

    def test_malformed_policy_rejected(self):
        valid, reason = rule_engine.validate_reminder_policy({"max_reminders": "not-a-number"})
        self.assertFalse(valid)

    def test_well_formed_policy_accepted(self):
        valid, reason = rule_engine.validate_reminder_policy(
            {"enabled": True, "first_reminder_delay_minutes": 120, "max_reminders": 3})
        self.assertTrue(valid)

    def test_escalate_stale_incidents_skips_unconfigured_business(self):
        incidents = [{"id": "inc-1", "tenant_id": "tenant-1", "business_id": "nughe_farms",
            "branch_id": "warri", "submission_id": "sub-1", "created_at": "2020-01-01T00:00:00Z"}]
        with patch("rule_engine.rest_get", new_callable=AsyncMock, side_effect=[incidents, []]) as rget, \
             patch("rule_engine.rest_post", new_callable=AsyncMock) as rpost, \
             patch("rule_engine.rest_patch", new_callable=AsyncMock) as rpatch, \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock) as create_task:
            result = asyncio.run(rule_engine.escalate_stale_incidents())
        self.assertEqual(result["escalated"], [])
        self.assertEqual(result["skipped_unconfigured"], ["inc-1"])
        create_task.assert_not_called()
        rpatch.assert_not_called()


class MissingDailyReportTests(unittest.TestCase):
    # item 9: missing required report -> follow-up task only if a
    # configured reporting rule exists
    def test_no_configured_policy_takes_no_action(self):
        with patch("rule_engine.rest_get", new_callable=AsyncMock, return_value=[]) as rget, \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock) as create_task:
            result = asyncio.run(rule_engine.check_missing_daily_report(
                "tenant-1", "nughe_farms", "warri", "amos-emp", "2026-09-16"))
        self.assertFalse(result["applied"])
        create_task.assert_not_called()

    def test_configured_policy_before_expected_hour_takes_no_action(self):
        policy_rows = [{"value": {"enabled": True, "expected_by_hour": 18}}]
        now = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
        with patch("rule_engine.rest_get", new_callable=AsyncMock, return_value=policy_rows), \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock) as create_task:
            result = asyncio.run(rule_engine.check_missing_daily_report(
                "tenant-1", "nughe_farms", "warri", "amos-emp", "2026-09-16", now=now))
        self.assertFalse(result["applied"])
        create_task.assert_not_called()

    def test_report_already_submitted_takes_no_action(self):
        async def fake_get(path, params):
            if path == "/rest/v1/biz_setting_versions":
                return [{"value": {"enabled": True, "expected_by_hour": 8}}]
            if path == "/rest/v1/biz_submissions":
                return [{"id": "sub-1"}]
            return []
        now = datetime(2026, 9, 16, 20, tzinfo=timezone.utc)
        with patch("rule_engine.rest_get", new_callable=AsyncMock, side_effect=fake_get), \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock) as create_task:
            result = asyncio.run(rule_engine.check_missing_daily_report(
                "tenant-1", "nughe_farms", "warri", "amos-emp", "2026-09-16", now=now))
        self.assertFalse(result["applied"])
        create_task.assert_not_called()

    def test_configured_policy_past_hour_no_report_creates_task(self):
        async def fake_get(path, params):
            if path == "/rest/v1/biz_setting_versions":
                return [{"value": {"enabled": True, "expected_by_hour": 8}}]
            if path == "/rest/v1/biz_submissions":
                return []
            return []
        now = datetime(2026, 9, 16, 20, tzinfo=timezone.utc)
        with patch("rule_engine.rest_get", new_callable=AsyncMock, side_effect=fake_get), \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock,
                   return_value={"id": "task-missing-1"}) as create_task:
            result = asyncio.run(rule_engine.check_missing_daily_report(
                "tenant-1", "nughe_farms", "warri", "amos-emp", "2026-09-16", now=now))
        self.assertTrue(result["applied"])
        self.assertEqual(result["task_id"], "task-missing-1")
        kwargs = create_task.call_args.kwargs
        self.assertEqual(kwargs["dedupe_key"], "missing_report:nughe_farms:warri:amos-emp:2026-09-16")


class SensitiveActionBoundaryTests(unittest.TestCase):
    # item 10: routine business-rule task types are never sensitive actions,
    # and apply_rules never touches the approval gate for them
    def test_routine_task_types_are_not_sensitive_actions(self):
        import task_engine
        routine_types = ("evidence_request_crate", "evidence_request_mortality",
            "bird_care_followup", "mortality_escalation", "missing_report_followup")
        for task_type in routine_types:
            self.assertNotIn(task_type, task_engine.SENSITIVE_ACTIONS)

    def test_apply_rules_never_calls_approval_gate(self):
        with patch("rule_engine.evidence_store.create_requirement", new_callable=AsyncMock,
                    return_value={"id": "ev-1"}), \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock, return_value={"id": "task-1"}), \
             patch("rule_engine.task_engine.request_approval", new_callable=AsyncMock) as request_approval, \
             patch("rule_engine.task_engine.enforce_approval", new_callable=AsyncMock) as enforce_approval, \
             patch("rule_engine.rest_get", new_callable=AsyncMock, return_value=[]), \
             patch("rule_engine.rest_post", new_callable=AsyncMock), \
             patch("rule_engine.rest_patch", new_callable=AsyncMock), \
             patch("rule_engine._ensure_reminder", new_callable=AsyncMock,
                   return_value={"reminder_id": "r1", "policy_configured": False}), \
             patch("rule_engine.notifier.queue_message", new_callable=AsyncMock, return_value={"id": "m1"}):
            asyncio.run(rule_engine.apply_rules("tenant-1", "nughe_farms", "warri", "amos-emp",
                "submission-1", {"fields": {"crates": 26, "mortality_count": 3}}, inbox_id="inbox-1"))
        request_approval.assert_not_called()
        enforce_approval.assert_not_called()


if __name__ == "__main__":
    unittest.main()
