import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import notifier
import outbound_whatsapp


class QueueMessageTests(unittest.TestCase):
    # 4. crate report missing image queues exactly one evidence request
    def test_queues_with_known_provider_sender(self):
        with patch("notifier.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [{"provider_sender": "+12025550123"}],
                        [{"id": "msg-1", "status": "queued", "provider_sender": "+12025550123"}]]) as rget, \
             patch("notifier.rest_post", new_callable=AsyncMock) as rpost:
            message = asyncio.run(notifier.queue_message("tenant-1", "nughe_farms", "warri", "amos-emp",
                "task-1", "evidence_request_crate", "Please send a clear photo of today's egg crates."))
        self.assertEqual(message["status"], "queued")
        posted = rpost.call_args.args[1][0]
        self.assertEqual(posted["provider_sender"], "+12025550123")
        self.assertEqual(posted["idempotency_key"], "task-1:evidence_request_crate")

    # 6. Phillip: task created but no delivery attempted without a confirmed number
    def test_no_confirmed_identity_recorded_as_skipped(self):
        with patch("notifier.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [], [{"id": "msg-2", "status": "skipped_no_identity", "provider_sender": None}]]), \
             patch("notifier.rest_post", new_callable=AsyncMock) as rpost:
            message = asyncio.run(notifier.queue_message("tenant-1", "nughe_farms", "warri", "phillip-emp",
                "task-2", "bird_care_followup", "A bird mortality was reported."))
        self.assertEqual(message["status"], "skipped_no_identity")
        posted = rpost.call_args.args[1][0]
        self.assertIsNone(posted["provider_sender"])
        self.assertEqual(posted["status"], "skipped_no_identity")

    # idempotent: re-queuing the same (related_task_id, message_type) reuses the row
    def test_idempotent_reuses_existing_queued_row(self):
        with patch("notifier.rest_get", new_callable=AsyncMock,
                    return_value=[{"id": "existing-msg", "status": "queued"}]) as rget, \
             patch("notifier.rest_post", new_callable=AsyncMock) as rpost:
            message = asyncio.run(notifier.queue_message("tenant-1", "nughe_farms", "warri", "amos-emp",
                "task-1", "evidence_request_crate", "text"))
        self.assertEqual(message["id"], "existing-msg")
        rpost.assert_not_called()


class DispatchQueuedMessageTests(unittest.TestCase):
    # 7. outbound WhatsApp API mocked successfully (via dispatch)
    def test_successful_dispatch_marks_sent(self):
        row = {"id": "msg-1", "status": "queued", "provider_sender": "+12025550123",
            "message_text": "hello"}
        with patch("notifier.outbound_whatsapp.send_text_message", new_callable=AsyncMock,
                    return_value={"provider_message_id": "wamid.1"}) as send, \
             patch("notifier.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(notifier.dispatch_queued_message(row, "phone-id-1"))
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider_message_id"], "wamid.1")
        body = rpatch.call_args.args[2]
        self.assertEqual(body["status"], "sent")

    # 8. failed outbound API response recorded safely
    def test_failed_dispatch_marks_failed_with_reason(self):
        row = {"id": "msg-1", "status": "queued", "provider_sender": "+12025550123",
            "message_text": "hello"}
        with patch("notifier.outbound_whatsapp.send_text_message", new_callable=AsyncMock,
                    side_effect=outbound_whatsapp.OutboundUnavailable("WhatsApp API returned status 400")), \
             patch("notifier.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(notifier.dispatch_queued_message(row, "phone-id-1"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("400", result["failure_reason"])
        body = rpatch.call_args.args[2]
        self.assertEqual(body["status"], "failed")

    def test_skipped_no_identity_is_never_dispatched(self):
        row = {"id": "msg-2", "status": "skipped_no_identity", "provider_sender": None, "message_text": "x"}
        with patch("notifier.outbound_whatsapp.send_text_message", new_callable=AsyncMock) as send, \
             patch("notifier.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(notifier.dispatch_queued_message(row, "phone-id-1"))
        send.assert_not_called()
        rpatch.assert_not_called()
        self.assertEqual(result["status"], "skipped_no_identity")


if __name__ == "__main__":
    unittest.main()
