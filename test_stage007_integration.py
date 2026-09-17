"""End-to-end wiring tests: message_processor -> rule_engine / evidence_store,
using the real Amos identity/assignment shape (nughe_farms/warri/manager).
rule_engine.apply_rules and evidence_store.handle_incoming_image themselves
are unit-tested in test_rule_engine.py / test_evidence_store.py; here we only
prove message_processor calls them correctly for the real business scope.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from message_processor import process_inbox_batch

AMOS_IDENTITY = [{"tenant_id": "00000000-0000-0000-0000-000000000001", "employee_id": "amos-emp",
    "provider": "whatsapp", "provider_sender": "+12025550123"}]
AMOS_ASSIGNMENT = [{"business_id": "nughe_farms", "branch_id": "warri"}]


def _client(inbox_rows, identities=AMOS_IDENTITY, assignments=AMOS_ASSIGNMENT):
    client = AsyncMock()
    async def fake_get(path, params=None):
        if path == "/rest/v1/biz_message_inbox":
            return httpx.Response(200, json=inbox_rows)
        if path == "/rest/v1/biz_sender_identities":
            wanted = (params or {}).get("provider_sender")
            if wanted == "eq.+12025550123":
                return httpx.Response(200, json=identities)
            return httpx.Response(200, json=[])
        if path == "/rest/v1/biz_assignments":
            return httpx.Response(200, json=assignments)
        if path == "/rest/v1/biz_submissions":
            return httpx.Response(200, json=[{"id": "submission-amos-1"}])
        raise AssertionError("unexpected GET " + path)
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=httpx.Response(201))
    client.patch = AsyncMock(return_value=httpx.Response(204))
    return client


class AmosNugheFarmsWarriTests(unittest.TestCase):
    # 1. Amos recognized as Nughe Farms / Warri manager: identity resolves to
    # exactly that business/branch, and the poultry-scoped rule engine fires.
    def test_amos_crate_report_triggers_poultry_rules(self):
        inbox_rows = [{"id": "inbox-amos-1", "provider": "whatsapp", "provider_account": "acct-1",
            "received_at": "2026-09-16T09:00:00Z",
            "payload": {"kind": "message", "event": {
                "from": "+12025550123", "type": "text", "text": {"body": "26 crates today"}}}}]
        client = _client(inbox_rows)
        with patch("message_processor.credentials", return_value=("https://example.supabase.co", "test-key")), \
             patch("message_processor.httpx.AsyncClient") as factory, \
             patch("rule_engine.evidence_store.create_requirement", new_callable=AsyncMock,
                   return_value={"id": "ev-1"}) as create_req, \
             patch("rule_engine.task_engine.create_task", new_callable=AsyncMock,
                   return_value={"id": "task-1"}) as create_task, \
             patch("rule_engine._ensure_reminder", new_callable=AsyncMock,
                   return_value={"reminder_id": "rem-1", "policy_configured": False}) as ensure_reminder, \
             patch("rule_engine.notifier.queue_message", new_callable=AsyncMock,
                   return_value={"id": "msg-1", "status": "skipped_no_identity"}) as queue_message, \
             patch("rule_engine._resolve_phone_number_id", new_callable=AsyncMock,
                   return_value="acct-1"):
            factory.return_value.__aenter__.return_value = client
            summary = asyncio.run(process_inbox_batch())
        self.assertEqual(summary["submitted"], 1)
        create_task.assert_called_once()
        ensure_reminder.assert_called_once()
        queue_message.assert_called_once()
        submitted = client.post.call_args.kwargs["json"][0]
        self.assertEqual(submitted["business_id"], "nughe_farms")
        self.assertEqual(submitted["branch_id"], "warri")
        self.assertEqual(submitted["kind"], "poultry_daily_report")
        self.assertEqual(submitted["payload"]["parsed"]["fields"]["crates"], 26.0)
        create_req.assert_called_once()
        create_task.assert_called_once()

    # 10 & 11 (wiring): an image from Amos routes to evidence handling, not a submission.
    def test_amos_image_routes_to_evidence_not_submission(self):
        inbox_rows = [{"id": "inbox-amos-2", "provider": "whatsapp", "provider_account": "acct-1",
            "received_at": "2026-09-16T09:05:00Z",
            "payload": {"kind": "message", "event": {
                "from": "+12025550123", "type": "image",
                "id": "wamid.img1", "timestamp": "1758000300",
                "image": {"id": "media-crates-1", "mime_type": "image/jpeg", "caption": "today's crates"}}}}]
        client = _client(inbox_rows)
        with patch("message_processor.credentials", return_value=("https://example.supabase.co", "test-key")), \
             patch("message_processor.httpx.AsyncClient") as factory, \
             patch("evidence_store.handle_incoming_image", new_callable=AsyncMock,
                   return_value={"linked_evidence_id": "ev-1", "ambiguous_candidate_ids": []}) as handle_image:
            factory.return_value.__aenter__.return_value = client
            summary = asyncio.run(process_inbox_batch())
        self.assertEqual(summary["images_linked"], 1)
        self.assertEqual(summary["submitted"], 0)
        client.post.assert_not_called()
        handle_image.assert_called_once_with(
            "00000000-0000-0000-0000-000000000001", "nughe_farms", "warri", "inbox-amos-2",
            {"from": "+12025550123", "type": "image", "id": "wamid.img1", "timestamp": "1758000300",
             "image": {"id": "media-crates-1", "mime_type": "image/jpeg", "caption": "today's crates"}},
            employee_id="amos-emp")


if __name__ == "__main__":
    unittest.main()
