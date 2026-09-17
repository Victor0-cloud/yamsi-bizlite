import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import task_engine


class CreateTaskIdempotencyTests(unittest.TestCase):
    # 9. duplicate processing must not create duplicate tasks
    def test_existing_task_for_submission_is_reused(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock,
                    return_value=[{"id": "task-existing"}]) as rget, \
             patch("task_engine.rest_post", new_callable=AsyncMock) as rpost:
            task = asyncio.run(task_engine.create_task("tenant-1", "nughe_farms", "warri",
                "evidence_request", "Send a photo", created_by="rule_engine", source="rule_engine",
                related_submission_id="sub-1"))
        self.assertEqual(task["id"], "task-existing")
        rpost.assert_not_called()

    def test_new_task_created_when_none_exists(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [{"id": "task-new"}]]) as rget, \
             patch("task_engine.rest_post", new_callable=AsyncMock) as rpost:
            task = asyncio.run(task_engine.create_task("tenant-1", "nughe_farms", "warri",
                "evidence_request", "Send a photo", created_by="rule_engine", source="rule_engine",
                related_submission_id="sub-1"))
        self.assertEqual(task["id"], "task-new")
        rpost.assert_called_once()
        self.assertEqual(rpost.call_args.kwargs["params"], {"on_conflict": "related_submission_id,task_type"})


class TaskStateMachineTests(unittest.TestCase):
    def test_valid_transition(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock, return_value=[{"status": "pending"}]), \
             patch("task_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            asyncio.run(task_engine.transition_task("task-1", "acknowledged"))
        rpatch.assert_called_once()
        self.assertEqual(rpatch.call_args.args[2]["status"], "acknowledged")

    def test_illegal_transition_rejected(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock, return_value=[{"status": "completed"}]), \
             patch("task_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            with self.assertRaises(ValueError):
                asyncio.run(task_engine.transition_task("task-1", "pending"))
        rpatch.assert_not_called()

    def test_completion_records_completed_by_and_timestamp(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock, return_value=[{"status": "in_progress"}]), \
             patch("task_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            asyncio.run(task_engine.transition_task("task-1", "completed", completed_by="emp-1"))
        body = rpatch.call_args.args[2]
        self.assertEqual(body["completed_by"], "emp-1")
        self.assertIn("completed_at", body)


class AuthorizationBoundaryTests(unittest.TestCase):
    # 16. unauthorized employee cannot cross business/branch boundaries
    def test_authorized_scope_passes(self):
        task_engine.assert_employee_authorized([("nughe_farms", "warri")], "nughe_farms", "warri")

    def test_unassigned_scope_blocked(self):
        with self.assertRaises(PermissionError):
            task_engine.assert_employee_authorized([("nughe_farms", "warri")], "amose_table_water", "asaba")


class ApprovalGateTests(unittest.TestCase):
    def test_unrecognized_action_type_rejected(self):
        with self.assertRaises(ValueError):
            asyncio.run(task_engine.request_approval("tenant-1", "not_a_real_action", "owner", "test"))

    def test_request_approval_creates_pending_row(self):
        with patch("task_engine.rest_post", new_callable=AsyncMock) as rpost, \
             patch("task_engine.rest_get", new_callable=AsyncMock,
                    return_value=[{"id": "req-1", "status": "pending"}]) as rget:
            request = asyncio.run(task_engine.request_approval(
                "tenant-1", "salary_change", "owner", "Raise Amos's monthly rate"))
        self.assertEqual(request["status"], "pending")
        rpost.assert_called_once()

    # 14. sensitive action without approval: blocked
    def test_enforce_approval_blocks_when_pending(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock, return_value=[{"status": "pending"}]):
            with self.assertRaises(task_engine.ApprovalRequired):
                asyncio.run(task_engine.enforce_approval("req-1"))

    def test_enforce_approval_blocks_when_missing(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock, return_value=[]):
            with self.assertRaises(task_engine.ApprovalRequired):
                asyncio.run(task_engine.enforce_approval("req-missing"))

    # 15. approved sensitive action permitted only through the defined gate
    def test_enforce_approval_passes_when_approved(self):
        with patch("task_engine.rest_get", new_callable=AsyncMock, return_value=[{"status": "approved"}]):
            asyncio.run(task_engine.enforce_approval("req-1"))  # must not raise

    def test_decide_approval_updates_status(self):
        with patch("task_engine.rest_patch", new_callable=AsyncMock) as rpatch:
            asyncio.run(task_engine.decide_approval("req-1", "owner-emp", True, notes="ok"))
        body = rpatch.call_args.args[2]
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["decided_by"], "owner-emp")


if __name__ == "__main__":
    unittest.main()
