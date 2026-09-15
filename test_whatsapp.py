import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from app import app
from whatsapp_webhook import extract_events, save_events

class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{"WHATSAPP_VERIFY_TOKEN":"test-verify","WHATSAPP_APP_SECRET":"test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client=TestClient(app)
        self.payload={"object":"whatsapp_business_account","entry":[{"changes":[{"field":"messages","value":{
            "metadata":{"phone_number_id":"test-account"},
            "messages":[{"id":"test-message","from":"test-sender","type":"text","text":{"body":"test fixture"}}]}}]}]}
    def send(self,payload):
        raw=json.dumps(payload).encode()
        sig="sha256="+hmac.new(b"test-secret",raw,hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/whatsapp",content=raw,headers={"x-hub-signature-256":sig})
    def test_handshake_plain_text(self):
        r=self.client.get("/webhooks/whatsapp",params={"hub.mode":"subscribe","hub.verify_token":"test-verify","hub.challenge":"12345"})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.text,"12345")
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
    def test_wrong_token(self):
        r=self.client.get("/webhooks/whatsapp",params={"hub.mode":"subscribe","hub.verify_token":"wrong","hub.challenge":"1"})
        self.assertEqual(r.status_code,403)
    def test_wrong_mode(self):
        r=self.client.get("/webhooks/whatsapp",params={"hub.mode":"other","hub.verify_token":"test-verify","hub.challenge":"1"})
        self.assertEqual(r.status_code,403)
    def test_missing_configuration(self):
        with patch.dict(os.environ,{"WHATSAPP_VERIFY_TOKEN":"","WHATSAPP_APP_SECRET":""}):
            self.assertEqual(self.client.get("/webhooks/whatsapp").status_code,503)
            self.assertEqual(self.send(self.payload).status_code,503)
    def test_missing_challenge(self):
        self.assertEqual(self.client.get("/webhooks/whatsapp",params={"hub.mode":"subscribe","hub.verify_token":"test-verify"}).status_code,400)
    def test_valid_message(self):
        with patch("whatsapp_webhook.save_events",new_callable=AsyncMock) as save:
            self.assertEqual(self.send(self.payload).status_code,200)
            self.assertEqual(save.call_args.args[0][0]["payload"]["kind"],"message")
    def test_unsigned_rejected(self):
        with patch("whatsapp_webhook.save_events",new_callable=AsyncMock) as save:
            self.assertEqual(self.client.post("/webhooks/whatsapp",json=self.payload).status_code,403)
            save.assert_not_called()
    def test_tampered_rejected(self):
        self.assertEqual(self.client.post("/webhooks/whatsapp",json=self.payload,headers={"x-hub-signature-256":"sha256=bad"}).status_code,403)
    def test_malformed(self):
        self.assertEqual(self.send({"object":"whatsapp_business_account","entry":"invalid"}).status_code,400)
    def test_unsupported_acknowledged(self):
        with patch("whatsapp_webhook.save_events",new_callable=AsyncMock) as save:
            self.assertEqual(self.send({"object":"other"}).json()["events"],0)
            save.assert_not_called()
    def test_statuses_distinct(self):
        value=self.payload["entry"][0]["changes"][0]["value"]
        value["statuses"]=[{"id":"test-message","status":"sent","timestamp":"1"},{"id":"test-message","status":"delivered","timestamp":"2"}]
        rows=extract_events(self.payload)
        self.assertEqual(len(rows),3)
        self.assertEqual(len({r["provider_event_id"] for r in rows}),3)
    def test_rebatch_identity(self):
        rows=extract_events(self.payload)
        self.payload["entry"][0]["changes"][0]["value"]["messages"] *= 2
        self.assertEqual(extract_events(self.payload),rows)
    def test_failure_requests_retry(self):
        from fastapi import HTTPException
        with patch("whatsapp_webhook.save_events",new_callable=AsyncMock,side_effect=HTTPException(503,"retry")):
            self.assertEqual(self.send(self.payload).status_code,503)
    def test_oversized(self):
        self.assertEqual(self.client.post("/webhooks/whatsapp",content=b"x"*(1024*1024+1)).status_code,413)

class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_conflict_request(self):
        client=AsyncMock()
        client.post.return_value=httpx.Response(201)
        with patch("whatsapp_webhook.credentials",return_value=("https://example.supabase.co","test-key")), patch("whatsapp_webhook.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value=client
            await save_events([{"provider":"whatsapp"}])
            kwargs=client.post.call_args.kwargs
            self.assertEqual(kwargs["params"]["on_conflict"],"provider,provider_account,provider_event_id")
            self.assertIn("ignore-duplicates",kwargs["headers"]["Prefer"])
    async def test_database_denial(self):
        from fastapi import HTTPException
        client=AsyncMock()
        client.post.return_value=httpx.Response(403)
        with patch("whatsapp_webhook.credentials",return_value=("https://example.supabase.co","test-key")), patch("whatsapp_webhook.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value=client
            with self.assertRaises(HTTPException) as ctx:
                await save_events([{}])
            self.assertEqual(ctx.exception.status_code,503)
