"""Stage 2 tests: production-ready live WhatsApp integration.

Unit tests use the same mocking conventions as the rest of the suite
(patch httpx.AsyncClient with an AsyncMock client; real httpx.Response
objects for payloads; patch.dict for environment isolation). No test
touches a live database, makes a real Meta request, or uses a real
credential -- every secret-shaped value here is a local throwaway
fixture. Database-side behavior (registry, claim lease, delivery
monotonicity) is additionally proven by supabase/probes/
live_whatsapp_probes.sql against the local shadow database.
"""
import asyncio
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app as app_module
import message_processor
import notifier
import outbound_dispatch_worker
import outbound_whatsapp
import provider_accounts
import whatsapp_config
import whatsapp_webhook
from app import app
from supabase_backend import DatabaseUnavailable

TOKEN = "test-only-sentinel-token-value-0001"
SECRET = "test-only-sentinel-secret-value-0002"
VERIFY = "test-only-sentinel-verify-value-0003"
OWNER_KEY = "test-only-owner-key-0004"

FULL_ENV = {
    "WHATSAPP_VERIFY_TOKEN": VERIFY,
    "WHATSAPP_APP_SECRET": SECRET,
    "WHATSAPP_ACCESS_TOKEN": TOKEN,
    "WHATSAPP_WEBHOOK_BASE_URL": "https://example.invalid/webhooks/whatsapp",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SECRET_KEY": "test-only-service-key",
    "YAMSI_API_KEY": OWNER_KEY,
}

STATUS_ROW = {"id": "inbox-status-1", "provider": "whatsapp",
    "provider_account": "acct-1",
    "payload": {"kind": "status", "event": {
        "id": "wamid.1", "status": "delivered", "timestamp": "123"}}}


def _signed_payload(payload, secret=SECRET):
    raw = json.dumps(payload).encode()
    return raw, "sha256=" + hmac.new(
        secret.encode(), raw, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# 1. Configuration and readiness.
# ---------------------------------------------------------------------------

class ReadinessTests(unittest.TestCase):
    def _check(self, env):
        with patch.dict(os.environ, env, clear=True), \
             patch("whatsapp_config.credentials",
                   return_value=("https://example.supabase.co", "key")):
            return whatsapp_config.check_whatsapp_readiness()

    def test_fully_configured_reports_live_ready(self):
        result = self._check(FULL_ENV)
        self.assertTrue(result["live_ready"])
        self.assertTrue(result["inbound_ready"])
        self.assertTrue(result["outbound_ready"])
        self.assertTrue(result["database_ready"])
        self.assertEqual(result["missing"], [])

    def test_report_exposes_no_values(self):
        result = self._check(FULL_ENV)
        blob = json.dumps(result)
        for secret in (TOKEN, SECRET, VERIFY, OWNER_KEY):
            self.assertNotIn(secret, blob)

    def test_missing_secrets_fail_closed(self):
        env = dict(FULL_ENV)
        for key in ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
                "WHATSAPP_ACCESS_TOKEN", "YAMSI_API_KEY"):
            del env[key]
        result = self._check(env)
        self.assertFalse(result["live_ready"])
        self.assertFalse(result["inbound_ready"])
        self.assertFalse(result["outbound_ready"])
        for key in ("verify_token", "app_secret", "access_token",
                "owner_api_key"):
            self.assertIn(key, result["missing"])

    def test_missing_database_fails_closed(self):
        with patch.dict(os.environ, FULL_ENV, clear=True), \
             patch("whatsapp_config.credentials",
                   side_effect=DatabaseUnavailable("nope")):
            result = whatsapp_config.check_whatsapp_readiness()
        self.assertFalse(result["database_ready"])
        self.assertFalse(result["live_ready"])
        self.assertIn("supabase", result["missing"])

    def test_unsupported_graph_version_rejected(self):
        env = dict(FULL_ENV, WHATSAPP_GRAPH_API_VERSION="v99.0")
        result = self._check(env)
        self.assertFalse(result["outbound_ready"])
        self.assertFalse(result["live_ready"])
        self.assertIn("graph_api_version", result["missing"])

    def test_non_https_base_url_rejected(self):
        env = dict(FULL_ENV,
            WHATSAPP_WEBHOOK_BASE_URL="http://example.invalid/hook")
        result = self._check(env)
        self.assertFalse(result["inbound_ready"])
        self.assertIn("webhook_base_url", result["missing"])


class ReadinessEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_requires_owner_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": OWNER_KEY}):
            response = self.client.get("/internal/whatsapp-readiness")
        self.assertEqual(response.status_code, 401)
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/internal/whatsapp-readiness",
                headers={"x-yamsi-key": "anything"})
        self.assertEqual(response.status_code, 503)

    def test_reports_dimensions_without_values(self):
        with patch.dict(os.environ, FULL_ENV, clear=True), \
             patch("whatsapp_config.credentials",
                   return_value=("https://example.supabase.co", "key")):
            response = self.client.get("/internal/whatsapp-readiness",
                headers={"x-yamsi-key": OWNER_KEY})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ("inbound_ready", "outbound_ready", "database_ready",
                "live_ready", "checks", "missing"):
            self.assertIn(key, body)
        blob = json.dumps(body)
        for secret in (TOKEN, SECRET, VERIFY, OWNER_KEY):
            self.assertNotIn(secret, blob)

    def test_public_health_unchanged_and_safe(self):
        with patch.dict(os.environ, FULL_ENV, clear=True):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        blob = json.dumps(response.json())
        for secret in (TOKEN, SECRET, VERIFY, OWNER_KEY):
            self.assertNotIn(secret, blob)
        self.assertNotIn("readiness", blob)

    def test_provider_accounts_requires_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": OWNER_KEY}):
            response = self.client.get("/internal/provider-accounts",
                params={"tenant_id": "tenant-1"})
        self.assertEqual(response.status_code, 401)

    def test_provider_accounts_lists_scopes(self):
        rows = [{"business_id": "water", "branch_id": "asaba",
            "provider": "whatsapp", "provider_account": "acct-1",
            "enabled": True, "label": "Main line"}]
        with patch.dict(os.environ, {"YAMSI_API_KEY": OWNER_KEY}), \
             patch("provider_accounts.list_accounts",
                   new_callable=AsyncMock, return_value=rows):
            response = self.client.get("/internal/provider-accounts",
                headers={"x-yamsi-key": OWNER_KEY},
                params={"tenant_id": "tenant-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accounts"], rows)

    def test_recover_endpoint_requires_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": OWNER_KEY}):
            response = self.client.post("/internal/recover-stale-outbound",
                json={})
        self.assertEqual(response.status_code, 401)


# ---------------------------------------------------------------------------
# 2. Webhook authenticity and input safety.
# ---------------------------------------------------------------------------

class WebhookSignatureTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ,
            {"WHATSAPP_VERIFY_TOKEN": "v", "WHATSAPP_APP_SECRET": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)
        process = patch("message_processor.process_inbox_batch",
            new_callable=AsyncMock)
        process.start()
        self.addCleanup(process.stop)
        self.client = TestClient(app)
        self.payload = {"object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "acct-1"},
                "messages": [{"id": "m1", "from": "s",
                    "type": "text", "text": {"body": "hi"}}]}}]}]}

    def test_duplicated_signature_rejected(self):
        raw, sig = _signed_payload(self.payload)
        response = self.client.post("/webhooks/whatsapp", content=raw,
            headers=[("x-hub-signature-256", sig),
                ("x-hub-signature-256", sig)])
        self.assertEqual(response.status_code, 403)

    def test_malformed_signatures_rejected(self):
        raw, _sig = _signed_payload(self.payload)
        for bad in ("sha256=zzz", "Bearer abc", "not-a-signature",
                "sha256=" + "ab" * 31):
            response = self.client.post("/webhooks/whatsapp", content=raw,
                headers={"x-hub-signature-256": bad})
        self.assertEqual(response.status_code, 403)

    def test_missing_signature_rejected(self):
        raw, _sig = _signed_payload(self.payload)
        response = self.client.post("/webhooks/whatsapp", content=raw)
        self.assertEqual(response.status_code, 403)

    def test_malformed_json_rejected(self):
        raw = b'{"object": "whatsapp_business_account", broken'
        sig = "sha256=" + hmac.new(
            SECRET.encode(), raw, hashlib.sha256).hexdigest()
        with patch("whatsapp_webhook.save_events",
                new_callable=AsyncMock) as save:
            response = self.client.post("/webhooks/whatsapp", content=raw,
                headers={"x-hub-signature-256": sig})
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    def test_insert_failure_schedules_no_processing(self):
        from fastapi import HTTPException
        raw, sig = _signed_payload(self.payload)
        with patch("whatsapp_webhook.save_events", new_callable=AsyncMock,
                   side_effect=HTTPException(503, "retry")) as save, \
             patch("message_processor.process_inbox_batch",
                   new_callable=AsyncMock) as process:
            response = self.client.post("/webhooks/whatsapp", content=raw,
                headers={"x-hub-signature-256": sig})
        self.assertEqual(response.status_code, 503)
        save.assert_called_once()
        process.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Provider-account authorization.
# ---------------------------------------------------------------------------

class ProviderAccountTests(unittest.TestCase):
    def test_exact_enabled_match_authorizes(self):
        row = {"tenant_id": "t", "business_id": "water",
            "branch_id": "asaba", "provider": "whatsapp",
            "provider_account": "acct-1", "enabled": True}
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=[row]):
            self.assertTrue(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "asaba", "whatsapp", "acct-1")))

    def test_unknown_account_fails_closed(self):
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=[]):
            self.assertFalse(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "asaba", "whatsapp", "nope")))

    def test_disabled_account_fails_closed(self):
        row = {"tenant_id": "t", "business_id": "water",
            "branch_id": "asaba", "provider": "whatsapp",
            "provider_account": "acct-1", "enabled": False}
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=[row]):
            # Disabled rows are filtered server-side; an empty result
            # (or a disabled row, defensively) never authorizes.
            self.assertFalse(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "asaba", "whatsapp", "acct-1")))

    def test_cross_scope_account_fails_closed(self):
        row = {"tenant_id": "t", "business_id": "water",
            "branch_id": "warri", "provider": "whatsapp",
            "provider_account": "acct-1", "enabled": True}
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=[row]):
            self.assertFalse(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "asaba", "whatsapp", "acct-1")))

    def test_blank_inputs_never_authorize(self):
        with patch("provider_accounts.rest_get", new_callable=AsyncMock) \
                as rget:
            self.assertFalse(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "asaba", "whatsapp", "  ")))
        rget.assert_not_called()

    def test_multibranch_lookup_is_scope_filtered(self):
        asaba = {"tenant_id": "t", "business_id": "water",
            "branch_id": "asaba", "provider": "telegram",
            "provider_account": "bot", "enabled": True}
        warri = dict(asaba, branch_id="warri")
        seen = {}

        async def fake_get(path, params=None):
            seen.update(params)
            if params.get("branch_id") == "eq.warri":
                return [warri]
            return [asaba]

        with patch("provider_accounts.rest_get",
                   new_callable=AsyncMock, side_effect=fake_get):
            self.assertTrue(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "warri", "telegram", "bot")))
        self.assertEqual(seen.get("business_id"), "eq.water")
        self.assertEqual(seen.get("branch_id"), "eq.warri")
        self.assertEqual(seen.get("provider_account"), "eq.bot")

    def test_same_account_serves_two_branches_without_crosstalk(self):
        asaba = {"tenant_id": "t", "business_id": "water",
            "branch_id": "asaba", "provider": "telegram",
            "provider_account": "bot", "enabled": True}
        warri = dict(asaba, branch_id="warri")

        async def fake_get(path, params=None):
            if params.get("branch_id") == "eq.warri":
                return [warri]
            return [asaba]

        with patch("provider_accounts.rest_get",
                   new_callable=AsyncMock, side_effect=fake_get):
            self.assertTrue(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "warri", "telegram", "bot")))
            self.assertTrue(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "asaba", "telegram", "bot")))
            self.assertFalse(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "north", "telegram", "bot")))

    def test_unscoped_ambiguous_account_fails_closed(self):
        rows = [
            {"tenant_id": "t", "business_id": "water",
             "branch_id": "asaba", "provider": "telegram",
             "provider_account": "bot", "enabled": True},
            {"tenant_id": "t", "business_id": "water",
             "branch_id": "warri", "provider": "telegram",
             "provider_account": "bot", "enabled": True}]
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=rows):
            self.assertIsNone(asyncio.run(
                provider_accounts.find_account("t", "telegram", "bot")))

    def test_disabled_branch_row_fails_closed(self):
        row = {"tenant_id": "t", "business_id": "water",
            "branch_id": "warri", "provider": "telegram",
            "provider_account": "bot", "enabled": False}
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=[row]):
            self.assertFalse(asyncio.run(
                provider_accounts.is_account_authorized(
                    "t", "water", "warri", "telegram", "bot")))

    def test_submission_carries_route_snapshot(self):
        client = AsyncMock()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_message_inbox":
                return httpx.Response(200, json=[{
                    "id": "inbox-1", "provider": "whatsapp",
                    "provider_account": "acct-9",
                    "payload": {"kind": "message", "event": {
                        "from": "+12025550123", "type": "text",
                        "text": {"body": "Sold 50 bags at 500"}}}}])
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[
                    {"tenant_id": "t", "employee_id": "e"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=[
                    {"business_id": "water", "branch_id": "warri"}])
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": "sub-1"}])
            if path == "/rest/v1/biz_outbound_messages":
                return httpx.Response(200, json=[])
            raise AssertionError("unexpected GET " + path)

        async def fake_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                return httpx.Response(200, json={"status": "queued",
                    "review_ref": "YR-ABCD234EFG",
                    "submission_id": "sub-1", "submission_kind": "sale",
                    "request_key": "k", "notified": 1, "skipped": [],
                    "is_retry": False})
            return httpx.Response(201)

        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(side_effect=fake_post)
        client.patch = AsyncMock(return_value=httpx.Response(204))
        with patch("message_processor.credentials",
                   return_value=("https://example.supabase.co", "k")), \
             patch("message_processor.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            summary = asyncio.run(message_processor.process_inbox_batch())
        self.assertEqual(summary["submitted"], 1)
        submitted = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"][0].kwargs["json"][0]
        self.assertEqual(submitted["provider"], "whatsapp")
        self.assertEqual(submitted["provider_account"], "acct-9")


# ---------------------------------------------------------------------------
# 4. Status-event handling.
# ---------------------------------------------------------------------------

class StatusSanitizeTests(unittest.TestCase):
    def test_meta_error_reduced_to_code_and_title(self):
        self.assertEqual(
            message_processor._sanitize_status_error(
                [{"code": 131026, "title": "Message undeliverable",
                    "href": "https://example.invalid/docs",
                    "payload": {"raw": "must never leak"}}]),
            "131026:Message undeliverable")

    def test_code_only_without_title(self):
        self.assertEqual(
            message_processor._sanitize_status_error([{"code": 131030}]),
            "131030")

    def test_unusable_errors_yield_none(self):
        self.assertIsNone(message_processor._sanitize_status_error(None))
        self.assertIsNone(message_processor._sanitize_status_error([]))
        self.assertIsNone(
            message_processor._sanitize_status_error([{"nope": 1}]))
        self.assertIsNone(
            message_processor._sanitize_status_error(
                [{"code": True, "title": "x"}]))


class StatusProcessingTests(unittest.TestCase):
    def _client(self, post_response):
        client = AsyncMock()
        client.post = AsyncMock(return_value=post_response)
        client.patch = AsyncMock(return_value=httpx.Response(204))
        return client

    def test_delivered_state_correlates_without_submission(self):
        client = self._client(httpx.Response(200, json={"status": "ok",
            "applied": True, "previous": "sent", "current": "delivered",
            "message_id": "row-1", "is_retry": False}))
        summary = {"scanned": 1, "submitted": 0, "unmatched": 0,
            "skipped_non_message": 0, "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0,
            "failed": 0, "reviews_confirmed": 0, "reviews_rejected": 0,
            "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0,
            "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0}
        asyncio.run(message_processor._process_status(
            client, dict(STATUS_ROW), summary))
        posts = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/rpc/amose_apply_delivery_status"]
        self.assertEqual(len(posts), 1)
        body = posts[0].kwargs["json"]
        self.assertEqual(body["p_provider"], "whatsapp")
        self.assertEqual(body["p_provider_account"], "acct-1")
        self.assertEqual(body["p_provider_message_id"], "wamid.1")
        self.assertEqual(body["p_delivery_state"], "delivered")
        submissions = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(submissions, [])
        confirms = [c for c in client.post.call_args_list
            if "confirm" in c.args[0] or "posting" in c.args[0]]
        self.assertEqual(confirms, [])
        self.assertEqual(client.patch.call_args.kwargs["json"],
            {"status": "processed"})

    def test_failed_state_carries_sanitized_code(self):
        client = self._client(httpx.Response(200, json={"status": "ok",
            "applied": True, "previous": "sent", "current": "failed",
            "message_id": "row-1", "is_retry": False}))
        row = {"id": "inbox-9", "provider": "whatsapp",
            "provider_account": "acct-1",
            "payload": {"kind": "status", "event": {
                "id": "wamid.9", "status": "failed", "timestamp": "5",
                "errors": [{"code": 131026,
                    "title": "Recipient is using an old version",
                    "details": "must never be stored"}]}}}
        summary = {"scanned": 1, "submitted": 0, "unmatched": 0,
            "skipped_non_message": 0, "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0,
            "failed": 0, "reviews_confirmed": 0, "reviews_rejected": 0,
            "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0,
            "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0}
        asyncio.run(message_processor._process_status(client, row, summary))
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(
            body["p_error_code"], "131026:Recipient is using an old version")
        self.assertNotIn("details", body["p_error_code"])

    def test_unknown_message_is_harmless(self):
        client = self._client(httpx.Response(200, json={"status": "ok",
            "applied": False, "reason": "unknown_message",
            "is_retry": False}))
        summary = {"scanned": 1, "submitted": 0, "unmatched": 0,
            "skipped_non_message": 0, "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0,
            "failed": 0, "reviews_confirmed": 0, "reviews_rejected": 0,
            "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0,
            "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0}
        asyncio.run(message_processor._process_status(
            client, dict(STATUS_ROW), summary))
        self.assertEqual(client.patch.call_args.kwargs["json"],
            {"status": "processed"})
        self.assertEqual(summary["failed"], 0)

    def test_malformed_status_fails_without_rpc(self):
        client = self._client(httpx.Response(200, json={}))
        row = {"id": "inbox-9", "provider": "whatsapp",
            "provider_account": "acct-1",
            "payload": {"kind": "status", "event": {"id": "wamid.9"}}}
        summary = {"scanned": 1, "submitted": 0, "unmatched": 0,
            "skipped_non_message": 0, "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0,
            "failed": 0, "reviews_confirmed": 0, "reviews_rejected": 0,
            "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0,
            "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0}
        asyncio.run(message_processor._process_status(client, row, summary))
        client.post.assert_not_called()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(client.patch.call_args.kwargs["json"]["status"],
            "failed")

    def test_malformed_rpc_response_is_retryable(self):
        client = self._client(httpx.Response(200, json={"bogus": True}))
        summary = {"scanned": 1, "submitted": 0, "unmatched": 0,
            "skipped_non_message": 0, "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0,
            "failed": 0, "reviews_confirmed": 0, "reviews_rejected": 0,
            "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0,
            "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0}
        with self.assertRaises(DatabaseUnavailable):
            asyncio.run(message_processor._process_status(
                client, dict(STATUS_ROW), summary))


# ---------------------------------------------------------------------------
# 5. Outbound Cloud API client.
# ---------------------------------------------------------------------------

class OutboundClientTests(unittest.TestCase):
    def _run(self, response=None, side_effect=None, env_token=TOKEN):
        client = AsyncMock()
        if side_effect is not None:
            client.post = AsyncMock(side_effect=side_effect)
        else:
            client.post = AsyncMock(return_value=response)
        env = patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": env_token},
            clear=True)
        env.start()
        self.addCleanup(env.stop)
        factory_patch = patch("outbound_whatsapp.httpx.AsyncClient")
        factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        factory.return_value.__aenter__.return_value = client
        return client, factory

    def test_accepted_response_stores_provider_message_id(self):
        client, _factory = self._run(httpx.Response(200, json={
            "messages": [{"id": "wamid.OUT1"}]}))
        result = asyncio.run(outbound_whatsapp.send_text_message(
            "1350537361474836", "+2348012345678", "hello"))
        self.assertEqual(result["provider_message_id"], "wamid.OUT1")
        sent = client.post.call_args.kwargs["json"]
        self.assertEqual(sent["to"], "2348012345678")
        self.assertNotIn(TOKEN, json.dumps(sent))

    def test_no_redirects_and_bounded_timeout(self):
        _client, factory = self._run(httpx.Response(200, json={
            "messages": [{"id": "wamid.OUT1"}]}))
        asyncio.run(outbound_whatsapp.send_text_message(
            "acct-1", "234800", "hello"))
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["follow_redirects"], False)
        self.assertIsNotNone(kwargs["timeout"])

    def test_retryable_and_permanent_classification(self):
        retryable = [
            httpx.Response(429, json={"error": {"code": 4}}),
            httpx.Response(500, json={}),
            httpx.Response(503, json={}),
        ]
        for response in retryable:
            client, _factory = self._run(response)
            with self.assertRaises(
                    outbound_whatsapp.RetryableOutboundError,
                    msg=str(response.status_code)) as ctx:
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "234800", "hello"))
            self.assertTrue(ctx.exception.retryable)
            self.assertNotIn(TOKEN, str(ctx.exception))
        permanent = [
            httpx.Response(400, json={"error": {"code": 131030,
                "message": "Invalid recipient", "field": "to"}}),
            httpx.Response(401, json={}),
            httpx.Response(404, json={}),
        ]
        for response in permanent:
            client, _factory = self._run(response)
            with self.assertRaises(
                    outbound_whatsapp.PermanentOutboundError,
                    msg=str(response.status_code)) as ctx:
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "234800", "hello"))
            self.assertFalse(ctx.exception.retryable)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertNotIn("Invalid recipient", str(ctx.exception))

    def test_transport_failures_are_retryable_without_token(self):
        for effect in (httpx.ConnectError("down"),
                httpx.TimeoutException("slow"),
                httpx.ReadError("broken")):
            client, _factory = self._run(side_effect=effect)
            with self.assertRaises(
                    outbound_whatsapp.RetryableOutboundError) as ctx:
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "234800", "hello"))
            self.assertNotIn(TOKEN, str(ctx.exception))

    def test_recipient_validation_fails_closed(self):
        client, _factory = self._run(httpx.Response(200, json={
            "messages": [{"id": "wamid.OUT1"}]}))
        for bad in ("", "abc", "12", "+12", "0" * 30, "234 800", None):
            with self.assertRaises(
                    outbound_whatsapp.PermanentOutboundError, msg=str(bad)):
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", bad, "hello"))
        client.post.assert_not_called()

    def test_message_validation_fails_closed(self):
        client, _factory = self._run(httpx.Response(200, json={
            "messages": [{"id": "wamid.OUT1"}]}))
        for bad in ("", "   ", "x" * 4097, None):
            with self.assertRaises(
                    outbound_whatsapp.PermanentOutboundError):
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "234800", bad))
        client.post.assert_not_called()

    def test_unexpected_acceptance_shape_is_permanent(self):
        for response in (httpx.Response(200, json={"messages": []}),
                httpx.Response(200, json={"messages": [{"id": ""}]}),
                httpx.Response(200, json={"ok": True})):
            client, _factory = self._run(response)
            with self.assertRaises(
                    outbound_whatsapp.PermanentOutboundError) as ctx:
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "234800", "hello"))
            self.assertNotIn(TOKEN, str(ctx.exception))

    def test_unsupported_version_fails_closed(self):
        client, _factory = self._run(httpx.Response(200, json={
            "messages": [{"id": "wamid.OUT1"}]}))
        with patch.dict(os.environ,
                {"WHATSAPP_GRAPH_API_VERSION": "v99.0"}):
            with self.assertRaises(
                    outbound_whatsapp.OutboundNotConfigured):
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "234800", "hello"))
        client.post.assert_not_called()

    def test_blank_provider_account_fails_closed(self):
        client, _factory = self._run(httpx.Response(200, json={
            "messages": [{"id": "wamid.OUT1"}]}))
        with self.assertRaises(outbound_whatsapp.PermanentOutboundError):
            asyncio.run(outbound_whatsapp.send_text_message(
                "  ", "234800", "hello"))
        client.post.assert_not_called()

    def test_retryable_is_still_outbound_unavailable(self):
        self.assertTrue(issubclass(outbound_whatsapp.RetryableOutboundError,
            outbound_whatsapp.OutboundUnavailable))
        self.assertTrue(issubclass(outbound_whatsapp.PermanentOutboundError,
            outbound_whatsapp.OutboundUnavailable))


# ---------------------------------------------------------------------------
# 6. Durable claiming, retry, and recovery.
# ---------------------------------------------------------------------------

WORKER_ROW = {"id": "msg-1", "tenant_id": "tenant-1",
    "business_id": "water", "branch_id": "asaba", "status": "queued",
    "provider_sender": "+12025550123", "provider_account": "acct-1",
    "message_text": "hello"}


class WorkerRetryTests(unittest.TestCase):
    def _run(self, row, dispatch_result=None, dispatch_side_effect=None):
        with patch("outbound_dispatch_worker.rest_get",
                   new_callable=AsyncMock, return_value=[row]) as rget, \
             patch("outbound_dispatch_worker._claim",
                   new_callable=AsyncMock,
                   return_value={**row, "status": "sending"}) as claim, \
             patch("outbound_dispatch_worker.rest_patch",
                   new_callable=AsyncMock) as rpatch, \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message",
                   new_callable=AsyncMock, return_value=dispatch_result,
                   side_effect=dispatch_side_effect) as dispatch, \
             patch("outbound_dispatch_worker.retry_engine.record_success",
                   new_callable=AsyncMock) as record_success, \
             patch("outbound_dispatch_worker.retry_engine.record_failure",
                   new_callable=AsyncMock) as record_failure:
            summary = asyncio.run(
                outbound_dispatch_worker.dispatch_pending())
        return summary, dict(rget=rget, claim=claim, rpatch=rpatch,
            dispatch=dispatch, record_success=record_success,
            record_failure=record_failure)

    def test_retryable_failure_requeues_with_backoff(self):
        summary, mocks = self._run(dict(WORKER_ROW),
            dispatch_result={"status": "failed",
                "failure_reason": "failure=timeout", "retryable": True})
        self.assertEqual(summary, {"scanned": 1, "sent": 0, "failed": 0,
            "claim_conflicts": 0})
        body = mocks["rpatch"].call_args.args[2]
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["attempt_count"], 1)
        self.assertIn("next_attempt_at", body)
        self.assertNotIn("provider_account", body)
        mocks["record_failure"].assert_called_once()

    def test_backoff_grows_and_caps(self):
        self.assertEqual(
            outbound_dispatch_worker.retry_delay_minutes(1), 5)
        self.assertEqual(
            outbound_dispatch_worker.retry_delay_minutes(2), 10)
        self.assertEqual(
            outbound_dispatch_worker.retry_delay_minutes(3), 20)
        self.assertEqual(
            outbound_dispatch_worker.retry_delay_minutes(99), 240)

    def test_attempts_stop_at_maximum(self):
        summary, mocks = self._run(
            {**WORKER_ROW, "attempt_count": 4},
            dispatch_result={"status": "failed",
                "failure_reason": "failure=timeout", "retryable": True})
        self.assertEqual(summary["failed"], 1)
        body = mocks["rpatch"].call_args.args[2]
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["attempt_count"], 5)

    def test_permanent_failure_never_requeues(self):
        summary, mocks = self._run(dict(WORKER_ROW),
            dispatch_result={"status": "failed",
                "failure_reason": "failure=meta_refused status=400",
                "retryable": False})
        self.assertEqual(summary["failed"], 1)
        body = mocks["rpatch"].call_args.args[2]
        self.assertEqual(body["status"], "failed")
        self.assertNotIn("next_attempt_at", body)

    def test_future_retry_not_due_is_skipped(self):
        summary, mocks = self._run(
            {**WORKER_ROW, "next_attempt_at": "2999-01-01T00:00:00+00:00"},
            dispatch_result={"status": "sent"})
        self.assertEqual(summary, {"scanned": 1, "sent": 0, "failed": 0,
            "claim_conflicts": 0})
        mocks["dispatch"].assert_not_called()
        mocks["claim"].assert_not_called()

    def test_claim_marks_lease(self):
        response = httpx.Response(200, json=[{**WORKER_ROW,
            "status": "sending"}])
        with patch("outbound_dispatch_worker.rest_patch",
                   new_callable=AsyncMock,
                   return_value=response) as rpatch:
            claimed = asyncio.run(
                outbound_dispatch_worker._claim("msg-1"))
        self.assertEqual(claimed["status"], "sending")
        body = rpatch.call_args.args[2]
        self.assertIn("claimed_at", body)

    def test_refused_claim_counts_as_conflict(self):
        from supabase_backend import DatabaseUnavailable
        with patch("outbound_dispatch_worker.rest_patch",
                   new_callable=AsyncMock,
                   side_effect=DatabaseUnavailable("denied")):
            claimed = asyncio.run(
                outbound_dispatch_worker._claim("msg-1"))
        self.assertIsNone(claimed)

    def test_telegram_rows_never_claimed_by_whatsapp_worker(self):
        telegram_row = {**WORKER_ROW, "id": "tg-1",
            "provider": "telegram", "provider_sender": "7551230024",
            "message_type": "review_request"}

        async def server_filter(path, params):
            self.assertEqual(params.get("provider"), "eq.whatsapp")
            rows = [r for r in [WORKER_ROW, telegram_row]
                if params.get("provider") in (
                    None, "eq." + r.get("provider", "whatsapp"))]
            return [r for r in rows if r.get("status") == "queued"]

        with patch("outbound_dispatch_worker.rest_get",
                   new_callable=AsyncMock,
                   side_effect=server_filter) as rget, \
             patch("outbound_dispatch_worker._claim",
                   new_callable=AsyncMock) as claim, \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message",
                   new_callable=AsyncMock,
                   return_value={"status": "sent"}) as dispatch, \
             patch("outbound_dispatch_worker.retry_engine.record_success",
                   new_callable=AsyncMock), \
             patch("outbound_dispatch_worker.retry_engine.record_failure",
                   new_callable=AsyncMock):
            summary = asyncio.run(
                outbound_dispatch_worker.dispatch_pending())
        self.assertEqual(summary, {"scanned": 1, "sent": 1, "failed": 0,
            "claim_conflicts": 0})
        claimed_ids = [c.args[0] for c in claim.call_args_list]
        self.assertNotIn("tg-1", claimed_ids)
        for call in dispatch.call_args_list:
            self.assertNotEqual(call.args[0].get("id"), "tg-1")


class RecoverStaleTests(unittest.TestCase):
    def _run(self, response, stale_seconds=1800, limit=20):
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        with patch("outbound_dispatch_worker.credentials",
                   return_value=("https://example.supabase.co", "k")), \
             patch("outbound_dispatch_worker.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            result = asyncio.run(
                outbound_dispatch_worker.recover_stale_claims(
                    stale_seconds=stale_seconds, limit=limit))
        return result, client

    def test_recover_returns_rpc_result(self):
        body = {"status": "ok", "reclaimed": 2,
            "message_ids": ["a", "b"], "tenants": [], "is_retry": False}
        result, client = self._run(httpx.Response(200, json=body))
        self.assertEqual(result, body)
        posted = client.post.call_args
        self.assertEqual(posted.args[0],
            "/rest/v1/rpc/amose_reclaim_stale_outbound")
        self.assertEqual(posted.kwargs["json"],
            {"p_stale_seconds": 1800, "p_limit": 20})

    def test_recover_validates_window(self):
        with self.assertRaises(
                outbound_dispatch_worker.OutboundRecoveryError):
            asyncio.run(
                outbound_dispatch_worker.recover_stale_claims(
                    stale_seconds=10))

    def test_recover_refusal_raises(self):
        client = AsyncMock()
        client.post = AsyncMock(return_value=httpx.Response(
            400, json={"message": "MALFORMED: nope"}))
        with patch("outbound_dispatch_worker.credentials",
                   return_value=("https://example.supabase.co", "k")), \
             patch("outbound_dispatch_worker.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(
                    outbound_dispatch_worker.OutboundRecoveryError):
                asyncio.run(
                    outbound_dispatch_worker.recover_stale_claims())

    def test_recover_endpoint_wires_bounded_call(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": OWNER_KEY}), \
             patch("outbound_dispatch_worker.recover_stale_claims",
                   new_callable=AsyncMock,
                   return_value={"status": "ok", "reclaimed": 1,
                       "message_ids": ["a"], "tenants": [],
                       "is_retry": False}) as recover:
            client = TestClient(app)
            response = client.post("/internal/recover-stale-outbound",
                headers={"x-yamsi-key": OWNER_KEY}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reclaimed"], 1)
        recover.assert_called_once_with(stale_seconds=1800, limit=20)


# ---------------------------------------------------------------------------
# 7. Branch clarification before scope selection.
# ---------------------------------------------------------------------------

CLARIFICATION_INBOX = {"id": "inbox-8", "provider": "whatsapp",
    "provider_account": "acct-8",
    "payload": {"kind": "message", "event": {
        "from": "+12025550123", "type": "text",
        "text": {"body": "Sold 50 bags at 500"}}}}
CLARIFICATION_ASSIGNMENTS = [
    {"business_id": "water", "branch_id": "asaba"},
    {"business_id": "water", "branch_id": "warri"}]


class ClarificationTests(unittest.TestCase):
    def _client(self, post_outbound_status=201):
        client = AsyncMock()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_message_inbox":
                return httpx.Response(200, json=[dict(CLARIFICATION_INBOX)])
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[
                    {"tenant_id": "tenant-1", "employee_id": "emp-8"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(
                    200, json=list(CLARIFICATION_ASSIGNMENTS))
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": "sub-8"}])
            if path == "/rest/v1/biz_outbound_messages":
                return httpx.Response(200, json=[])
            raise AssertionError("unexpected GET " + path)

        async def fake_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/biz_outbound_messages":
                return httpx.Response(post_outbound_status)
            return httpx.Response(201)

        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(side_effect=fake_post)
        client.patch = AsyncMock(return_value=httpx.Response(204))
        return client

    def _run_batch(self, client):
        with patch("message_processor.credentials",
                   return_value=("https://example.supabase.co", "k")), \
             patch("message_processor.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            return asyncio.run(message_processor.process_inbox_batch())

    def test_multi_branch_sender_queues_exactly_one_clarification(self):
        client = self._client()
        summary = self._run_batch(client)
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["clarifications_queued"], 1)
        self.assertEqual(summary["failed"], 0)
        # No operational submission of any kind.
        submissions = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(submissions, [])
        postings = [c for c in client.post.call_args_list
            if "/rpc/" in c.args[0]
            and ("posting" in c.args[0] or "confirm" in c.args[0])]
        self.assertEqual(postings, [])
        queued = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_outbound_messages"]
        self.assertEqual(len(queued), 1)
        row = queued[0].kwargs["json"][0]
        self.assertEqual(row["message_type"], "branch_clarification")
        self.assertEqual(row["idempotency_key"],
            "inbox-8:branch_clarification")
        # The real sender is stored; nothing is guessed.
        self.assertEqual(row["recipient_employee_id"], "emp-8")
        self.assertEqual(row["provider_sender"], "+12025550123")
        self.assertEqual(row["provider_account"], "acct-8")
        self.assertIsNone(row["business_id"])
        self.assertIsNone(row["branch_id"])
        self.assertIsNone(row["related_task_id"])
        self.assertIn("ASABA:", row["message_text"])

    def test_repeat_delivery_does_not_duplicate(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=httpx.Response(200, json=[{"id": "existing"}]))
        client.post = AsyncMock()
        summary = {"scanned": 0, "submitted": 0, "unmatched": 0,
            "skipped_non_message": 0, "clarifications_queued": 0,
            "images_linked": 0, "images_unlinked": 0, "images_ambiguous": 0,
            "failed": 0, "reviews_confirmed": 0, "reviews_rejected": 0,
            "reviews_refused": 0, "reviews_failed": 0,
            "review_requests_queued": 0, "review_requests_failed": 0,
            "review_requests_already_queued": 0,
            "review_requests_no_reviewer": 0,
            "review_requests_unroutable": 0}
        queued = asyncio.run(message_processor._request_branch_selection(
            client, dict(CLARIFICATION_INBOX), "tenant-1", "emp-8",
            "+12025550123",
            [("water", "asaba"), ("water", "warri")], summary))
        self.assertFalse(queued)
        client.post.assert_not_called()
        self.assertEqual(summary["clarifications_queued"], 0)

    def test_refused_clarification_fails_closed_without_submission(self):
        client = self._client(post_outbound_status=400)
        summary = self._run_batch(client)
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["clarifications_queued"], 0)
        self.assertEqual(summary["failed"], 1)
        submissions = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(submissions, [])

    def test_clarification_retry_keeps_original_account(self):
        row = {"id": "msg-8", "tenant_id": "tenant-1",
            "business_id": None, "branch_id": None, "status": "queued",
            "provider_sender": "+12025550123", "provider_account": "acct-8",
            "message_text": "Please begin your message with ASABA:."}
        with patch("outbound_dispatch_worker.rest_get",
                   new_callable=AsyncMock, return_value=[row]), \
             patch("outbound_dispatch_worker._claim",
                   new_callable=AsyncMock,
                   return_value={**row, "status": "sending"}), \
             patch("outbound_dispatch_worker.rest_patch",
                   new_callable=AsyncMock) as rpatch, \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message",
                   new_callable=AsyncMock,
                   return_value={"status": "failed",
                       "failure_reason": "failure=timeout",
                       "retryable": True}), \
             patch("outbound_dispatch_worker.retry_engine.record_success",
                   new_callable=AsyncMock), \
             patch("outbound_dispatch_worker.retry_engine.record_failure",
                   new_callable=AsyncMock):
            summary = asyncio.run(
                outbound_dispatch_worker.dispatch_pending())
        self.assertEqual(summary, {"scanned": 1, "sent": 0, "failed": 0,
            "claim_conflicts": 0})
        body = rpatch.call_args.args[2]
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["attempt_count"], 1)
        self.assertNotIn("provider_account", body)
        self.assertNotIn("business_id", body)


# ---------------------------------------------------------------------------
# 12. Migration volatility contract: table-reading helpers must be STABLE.
# ---------------------------------------------------------------------------


class FunctionVolatilityTests(unittest.TestCase):
    HELPERS = ("_amose_provider_account_authorized",
        "_amose_provider_tenant_authorized")
    VOLATILE_DEFAULT = ("_amose_guard_submission_provider_account",
        "_amose_guard_outbound_provider_account",
        "amose_reclaim_stale_outbound", "amose_apply_delivery_status")

    @classmethod
    def _migration(cls):
        if not hasattr(cls, "_cached"):
            with open("supabase/migrations/"
                    "20260921000000_live_whatsapp_integration.sql",
                    encoding="utf-8") as handle:
                cls._cached = handle.read()
        return cls._cached

    @classmethod
    def _header(cls, name):
        text = cls._migration()
        start = text.index(
            "create or replace function public.%s(" % name)
        end = text.index("as $func$", start)
        return text[start:end]

    def test_no_immutable_function_in_migration(self):
        self.assertNotIn("immutable", self._migration().lower())

    def test_authorization_helpers_are_stable(self):
        for name in self.HELPERS:
            header = self._header(name)
            self.assertIn("\nstable\n", "\n" + header + "\n")
            lowered = header.lower()
            self.assertNotIn("immutable", lowered)
            self.assertNotIn("volatile", lowered)

    def test_helpers_keep_security_definer_and_search_path(self):
        for name in self.HELPERS:
            header = self._header(name).lower()
            self.assertIn("security definer", header)
            self.assertIn("set search_path = pg_catalog", header)

    def test_helpers_read_provider_registry(self):
        for name in self.HELPERS:
            header_start = self._migration().index(
                "create or replace function public.%s(" % name)
            body_end = self._migration().index("$func$;", header_start)
            body = self._migration()[header_start:body_end]
            self.assertIn("public.biz_provider_accounts", body)

    def test_guards_and_rpcs_stay_volatile_default(self):
        for name in self.VOLATILE_DEFAULT:
            header = self._header(name).lower()
            self.assertNotIn("immutable", header)
            self.assertNotRegex(header, r"(?m)^stable\s*$")
            self.assertIn("security definer", header)


# ---------------------------------------------------------------------------
# 13. Legacy-history foreign keys: NOT VALID preserves historical rows
# while new writes stay enforced, and the BEFORE triggers keep failing
# closed on unknown or disabled accounts.
# ---------------------------------------------------------------------------


class ProviderAccountConstraintTests(unittest.TestCase):
    FKS = ("biz_submissions_provider_account_fk",
        "biz_outbound_provider_account_fk")
    SCOPE_COLUMNS = ("tenant_id", "business_id", "branch_id",
        "provider", "provider_account")

    @classmethod
    def _migration(cls):
        return FunctionVolatilityTests._migration()

    @classmethod
    def _fk_block(cls, name):
        text = cls._migration()
        start = text.index("add constraint %s" % name)
        return text[start:text.index(";", start)]

    @classmethod
    def _body(cls, name):
        text = cls._migration()
        start = text.index(
            "create or replace function public.%s(" % name)
        end = text.index("$func$;", start)
        return text[start:end]

    def test_both_foreign_keys_are_not_valid(self):
        for name in self.FKS:
            with self.subTest(fk=name):
                block = self._fk_block(name).lower()
                self.assertIn("not valid", block)

    def test_not_valid_keys_still_pin_exact_scope(self):
        for name in self.FKS:
            with self.subTest(fk=name):
                block = self._fk_block(name)
                self.assertIn(
                    "references public.biz_provider_accounts", block)
                fk_part, ref_part = block.split("references")
                for column in self.SCOPE_COLUMNS:
                    self.assertIn(column, fk_part)
                    self.assertIn(column, ref_part)

    def test_validation_runs_only_after_reconciliation(self):
        for line in self._migration().splitlines():
            if "validate constraint" in line.lower():
                self.assertTrue(line.strip().startswith("--"), line)

    def test_guards_reject_unknown_or_disabled_accounts(self):
        submission = self._body(
            "_amose_guard_submission_provider_account")
        self.assertIn("_amose_provider_account_authorized(", submission)
        self.assertIn("raise exception 'UNAUTHORIZED", submission)
        outbound = self._body(
            "_amose_guard_outbound_provider_account")
        self.assertIn("_amose_provider_account_authorized(", outbound)
        self.assertIn("_amose_provider_tenant_authorized(", outbound)
        self.assertEqual(
            outbound.count("raise exception 'UNAUTHORIZED"), 2)
        lowered = self._migration().lower()
        self.assertIn(
            "before insert on public.biz_submissions", lowered)
        self.assertIn(
            "before insert or update on public.biz_outbound_messages",
            lowered)

    def test_registered_enabled_scope_authorizes(self):
        for name in ("_amose_provider_account_authorized",
                "_amose_provider_tenant_authorized"):
            with self.subTest(helper=name):
                body = self._body(name)
                self.assertIn("public.biz_provider_accounts", body)
                self.assertIn("a.enabled = true", body)

    def test_migration_preserves_history_without_seeding(self):
        lowered = self._migration().lower()
        self.assertNotRegex(lowered, r"(?m)^\s*delete\s+from\s")
        self.assertNotIn("truncate", lowered)
        self.assertNotIn(
            "insert into public.biz_provider_accounts", lowered)

    def test_later_migrations_leave_legacy_keys_alone(self):
        for name in ("20260922000000_telegram_full_intake.sql",
                "20260923000000_intake_correction_cancel.sql"):
            with self.subTest(migration=name):
                with open("supabase/migrations/" + name,
                        encoding="utf-8") as handle:
                    text = handle.read()
                self.assertNotIn("provider_account_fk", text)
                self.assertNotIn("validate constraint", text.lower())


# ---------------------------------------------------------------------------
# 14. Multi-branch provider accounts: one account may serve many scopes.
# ---------------------------------------------------------------------------


class MultibranchMigrationTests(unittest.TestCase):
    PATH = "supabase/migrations/20260924000000_multibranch_provider_accounts.sql"
    TENANT_KEY = ("biz_provider_accounts_tenant_id_provider_"
        "provider_account_key")

    @classmethod
    def _migration(cls, name=None):
        # PATH is already the complete repository-relative path; an
        # explicit name must be complete too (never prepended twice).
        with open(name or cls.PATH, encoding="utf-8") as handle:
            return handle.read()

    def test_drops_only_tenant_wide_uniqueness(self):
        import re
        text = self._migration()
        drops = re.findall(
            r"drop constraint if exists\s+(\w+)", text.lower())
        self.assertEqual(drops, [self.TENANT_KEY])

    def test_scoped_uniqueness_survives(self):
        old = self._migration(
            "supabase/migrations/"
            "20260921000000_live_whatsapp_integration.sql")
        self.assertIn(
            "unique (tenant_id, business_id, branch_id, provider,"
            " provider_account)",
            old)
        drops = " ".join(
            line for line in self._migration().lower().splitlines()
            if "drop constraint" in line)
        self.assertNotIn("business_id", drops)

    def test_tenant_helper_uses_existence_semantics(self):
        text = self._migration()
        start = text.index(
            "create or replace function public."
            "_amose_provider_tenant_authorized(")
        body = text[start:text.index("$func$;", start)]
        self.assertIn("select exists (", body)
        self.assertIn("a.enabled = true", body)
        self.assertIn("security definer", text[start:start + 400])
        self.assertIn("revoke execute on function public."
            "_amose_provider_tenant_authorized(uuid, text, text)",
            text)

    def test_migration_preserves_rows_and_keys(self):
        lowered = self._migration().lower()
        self.assertNotRegex(lowered, r"(?m)^\s*delete\s+from\s")
        self.assertNotIn("truncate", lowered)
        self.assertNotIn("insert into public.biz_provider_accounts",
            lowered)
        self.assertNotIn("validate constraint", lowered)
        self.assertNotIn("provider_account_fk", lowered)


if __name__ == "__main__":
    unittest.main()
