"""Stage 007B item 1: the webhook triggers processing automatically after a
durable inbox insert, without ever risking the insert itself, the signature
check, or the response to Meta. TestClient runs FastAPI background tasks
synchronously as part of the request, so these assertions observe the same
call that would happen in production.
"""
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app import app

PAYLOAD = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
    "metadata": {"phone_number_id": "test-account"},
    "messages": [{"id": "test-message", "from": "test-sender", "type": "text", "text": {"body": "hi"}}]}}]}]}


class AutomaticProcessingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"WHATSAPP_VERIFY_TOKEN": "test-verify", "WHATSAPP_APP_SECRET": "test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)

    def _send(self, payload):
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig})

    # 1. valid inbox event automatically reaches the processing path
    def test_valid_event_triggers_background_processing(self):
        with patch("whatsapp_webhook.save_events", new_callable=AsyncMock), \
             patch("message_processor.process_inbox_batch", new_callable=AsyncMock) as process:
            response = self._send(PAYLOAD)
        self.assertEqual(response.status_code, 200)
        process.assert_called_once()

    def test_no_events_does_not_trigger_processing(self):
        with patch("whatsapp_webhook.save_events", new_callable=AsyncMock), \
             patch("message_processor.process_inbox_batch", new_callable=AsyncMock) as process:
            response = self._send({"object": "other"})
        self.assertEqual(response.status_code, 200)
        process.assert_not_called()

    # 2. processing failure preserves the inbox event for retry -- the
    # background trigger swallows the exception; the webhook still responds
    # 200 (the durable insert already succeeded before this ran), and
    # nothing about the insert/signature path is affected.
    def test_processing_failure_does_not_break_webhook_response(self):
        with patch("whatsapp_webhook.save_events", new_callable=AsyncMock), \
             patch("message_processor.process_inbox_batch", new_callable=AsyncMock,
                   side_effect=RuntimeError("simulated downstream failure")) as process:
            response = self._send(PAYLOAD)
        self.assertEqual(response.status_code, 200)
        process.assert_called_once()

    def test_durable_insert_happens_before_processing_is_scheduled(self):
        call_order = []
        async def fake_save_events(rows):
            call_order.append("save_events")
        async def fake_process():
            call_order.append("process")
        with patch("whatsapp_webhook.save_events", side_effect=fake_save_events), \
             patch("message_processor.process_inbox_batch", side_effect=fake_process):
            self._send(PAYLOAD)
        self.assertEqual(call_order, ["save_events", "process"])


if __name__ == "__main__":
    unittest.main()
