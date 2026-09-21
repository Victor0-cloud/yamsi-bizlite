"""Stage 3 tests: safe production deployment of the live WhatsApp integration.

Covers the production-readiness layer only (production_readiness.py,
safe_logging.py, the new GET /internal/whatsapp/readiness endpoint, the
env-driven outbound timeout, and secret-safe logging in the webhook and
dispatch worker). Stage 1/2 behavior is covered by
test_daily_business_records.py and test_live_whatsapp.py and is not
re-asserted here beyond the production fail-closed boundaries.

Same conventions as the rest of the suite: patch httpx.AsyncClient with
an AsyncMock client, real httpx.Response objects, patch.dict for
environment isolation. No test touches a live database, makes a real
Meta request, or uses a real credential -- every secret-shaped value is
a local throwaway fixture. Registry counting is additionally proven by
supabase/probes/production_readiness_probes.sql against the local
shadow database.
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

import outbound_dispatch_worker
import outbound_whatsapp
import production_readiness
import provider_accounts
import safe_logging
import whatsapp_webhook
from app import app
from supabase_backend import DatabaseUnavailable

VERIFY = "test-only-verify-token-0001"
SECRET = "test-only-app-secret-0002"
TOKEN = "test-only-access-token-0003"
OWNER_KEY = "test-only-owner-key-0005"

FULL_ENV = {
    "WHATSAPP_VERIFY_TOKEN": VERIFY,
    "WHATSAPP_APP_SECRET": SECRET,
    "WHATSAPP_ACCESS_TOKEN": TOKEN,
    "WHATSAPP_WEBHOOK_BASE_URL": "https://example.invalid",
    "SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co",
    "SUPABASE_SECRET_KEY": "test-only-service-key-0004",
    "YAMSI_API_KEY": OWNER_KEY,
}

SECRETS = (VERIFY, SECRET, TOKEN, OWNER_KEY,
    "test-only-service-key-0004")


def _blob(value):
    return json.dumps(value, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# 1. Environment classification.
# ---------------------------------------------------------------------------

class ValidatorClassificationTests(unittest.TestCase):
    def _validate(self, env):
        with patch.dict(os.environ, env, clear=True):
            return production_readiness.validate_environment()

    def test_fully_configured_is_ready(self):
        result = self._validate(FULL_ENV)
        self.assertTrue(result["ready"])
        self.assertTrue(result["production_mode"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["malformed"], [])
        self.assertEqual(result["conflicting"], [])
        for name in production_readiness.REQUIRED_VARS:
            self.assertEqual(result["variables"][name], "ok")
        self.assertNotIn(" ".join(SECRETS), _blob(result))
        for secret in SECRETS:
            self.assertNotIn(secret, _blob(result))

    def test_each_required_variable_missing_fails_closed(self):
        names = list(production_readiness.REQUIRED_VARS) + [
            production_readiness.BASE_URL_VAR]
        for name in names:
            env = dict(FULL_ENV)
            env.pop(name, None)
            result = self._validate(env)
            self.assertFalse(result["ready"], msg=name)
            self.assertIn(name, result["missing"], msg=name)

    def test_blank_required_variable_counts_as_missing(self):
        env = dict(FULL_ENV, WHATSAPP_ACCESS_TOKEN="   ")
        result = self._validate(env)
        self.assertFalse(result["ready"])
        self.assertIn("WHATSAPP_ACCESS_TOKEN", result["missing"])

    def test_malformed_values_fail_closed(self):
        cases = [
            ("SUPABASE_URL", "http://abcdefghijklmnopqrst.supabase.co"),
            ("SUPABASE_URL", "https://short.supabase.co"),
            ("SUPABASE_URL", "https://example.invalid"),
            ("WHATSAPP_WEBHOOK_BASE_URL", "http://example.invalid/hook"),
            ("WHATSAPP_WEBHOOK_BASE_URL", "not-a-url"),
            ("WHATSAPP_VERIFY_TOKEN", "short"),
            ("WHATSAPP_APP_SECRET", "changeme"),
            ("WHATSAPP_ACCESS_TOKEN", "<WHATSAPP_ACCESS_TOKEN>"),
            ("YAMSI_API_KEY", "test"),
            ("SUPABASE_SECRET_KEY", "your_secret_key_here"),
        ]
        for name, bad in cases:
            env = dict(FULL_ENV)
            env[name] = bad
            result = self._validate(env)
            self.assertFalse(result["ready"], msg="%s=%r" % (name, bad))
            self.assertIn(name, result["malformed"], msg=name)

    def test_malformed_api_version_rejected(self):
        for bad in ("21.0", "v99.0", "latest", "v21"):
            env = dict(FULL_ENV, WHATSAPP_GRAPH_API_VERSION=bad)
            result = self._validate(env)
            self.assertFalse(result["ready"], msg=bad)
            self.assertIn("WHATSAPP_GRAPH_API_VERSION",
                result["malformed"])

    def test_malformed_env_mode_and_timeout_rejected(self):
        env = dict(FULL_ENV, WHATSAPP_ENV="staging")
        result = self._validate(env)
        self.assertIn("WHATSAPP_ENV", result["malformed"])
        for bad in ("abc", "0", "-5", "999"):
            env = dict(FULL_ENV, OUTBOUND_HTTP_TIMEOUT_SECONDS=bad)
            result = self._validate(env)
            self.assertFalse(result["ready"], msg=bad)
            self.assertIn("OUTBOUND_HTTP_TIMEOUT_SECONDS",
                result["malformed"], msg=bad)

    def test_conflicting_base_urls_reported(self):
        env = dict(FULL_ENV,
            WHATSAPP_WEBHOOK_BASE_URL="https://one.invalid",
            WEBHOOK_BASE_URL="https://two.invalid")
        result = self._validate(env)
        self.assertFalse(result["ready"])
        self.assertIn("WHATSAPP_WEBHOOK_BASE_URL", result["conflicting"])
        self.assertIn("WEBHOOK_BASE_URL", result["conflicting"])

    def test_matching_legacy_base_url_is_tolerated(self):
        env = dict(FULL_ENV, WEBHOOK_BASE_URL="https://example.invalid")
        result = self._validate(env)
        self.assertTrue(result["ready"])
        self.assertEqual(result["conflicting"], [])

    def test_legacy_fallback_warns_and_stays_not_ready(self):
        env = dict(FULL_ENV)
        del env["WHATSAPP_WEBHOOK_BASE_URL"]
        env["PUBLIC_BASE_URL"] = "https://legacy.invalid"
        result = self._validate(env)
        self.assertFalse(result["ready"])
        self.assertIn("WHATSAPP_WEBHOOK_BASE_URL", result["missing"])
        self.assertTrue(any("PUBLIC_BASE_URL" in warning
            or "deprecated" in warning for warning in result["warnings"]))

    def test_optional_unset_uses_defaults_and_stays_ready(self):
        env = dict(FULL_ENV)
        result = self._validate(env)
        self.assertEqual(
            result["variables"]["WHATSAPP_GRAPH_API_VERSION"], "defaulted")
        self.assertEqual(result["variables"]["WHATSAPP_ENV"], "defaulted")
        self.assertEqual(
            result["variables"]["OUTBOUND_HTTP_TIMEOUT_SECONDS"], "defaulted")
        self.assertTrue(result["ready"])

    def test_local_and_test_modes_are_reported(self):
        for mode in ("local", "test", "LOCAL"):
            env = dict(FULL_ENV, WHATSAPP_ENV=mode)
            result = self._validate(env)
            self.assertFalse(result["production_mode"], msg=mode)
            self.assertTrue(result["ready"])
            self.assertTrue(result["warnings"])

    def test_remediation_names_variables_only(self):
        env = {}
        result = self._validate(env)
        self.assertFalse(result["ready"])
        self.assertTrue(result["remediation"])
        for secret in SECRETS:
            self.assertNotIn(secret, _blob(result["remediation"]))


# ---------------------------------------------------------------------------
# 2. Readiness report.
# ---------------------------------------------------------------------------

class ReadinessReportTests(unittest.TestCase):
    def _report(self, env, tenant_id=None, accounts=None):
        rows = accounts if accounts is not None else []
        with patch.dict(os.environ, env, clear=True), \
             patch("provider_accounts.list_accounts",
                   new_callable=AsyncMock, return_value=rows):
            return asyncio.run(
                production_readiness.build_readiness_report(
                    tenant_id=tenant_id))

    def test_full_report_ready_with_account_count(self):
        rows = [{"enabled": True}, {"enabled": True}, {"enabled": False}]
        report = self._report(FULL_ENV, tenant_id="tenant-1",
            accounts=rows)
        self.assertTrue(report["ready"])
        self.assertTrue(report["production_mode"])
        for dimension in ("database", "provider", "webhook",
                "outbound_dispatch"):
            self.assertTrue(report["dimensions"][dimension]["ready"],
                msg=dimension)
        self.assertEqual(report["enabled_provider_account_count"], 2)
        for secret in SECRETS:
            self.assertNotIn(secret, _blob(report))

    def test_missing_tenant_leaves_count_null_with_warning(self):
        report = self._report(FULL_ENV)
        self.assertTrue(report["ready"])
        self.assertIsNone(report["enabled_provider_account_count"])
        self.assertTrue(any("tenant_id" in warning
            for warning in report["warnings"]))

    def test_unreachable_registry_never_raises(self):
        with patch.dict(os.environ, FULL_ENV, clear=True), \
             patch("provider_accounts.list_accounts",
                   new_callable=AsyncMock,
                   side_effect=DatabaseUnavailable("down")):
            report = asyncio.run(
                production_readiness.build_readiness_report(
                    tenant_id="tenant-1"))
        self.assertIsNone(report["enabled_provider_account_count"])
        self.assertTrue(any("unreachable" in warning
            for warning in report["warnings"]))

    def test_empty_environment_is_not_ready(self):
        report = self._report({})
        self.assertFalse(report["ready"])
        for dimension in ("database", "provider", "webhook",
                "outbound_dispatch"):
            self.assertFalse(report["dimensions"][dimension]["ready"],
                msg=dimension)
        self.assertTrue(report["missing"])
        self.assertTrue(report["remediation"])

    def test_report_is_deterministic(self):
        first = self._report(FULL_ENV, tenant_id="tenant-1")
        second = self._report(FULL_ENV, tenant_id="tenant-1")
        self.assertEqual(_blob(first), _blob(second))


# ---------------------------------------------------------------------------
# 3. Readiness endpoint: auth and redaction.
# ---------------------------------------------------------------------------

class ProductionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_requires_owner_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": OWNER_KEY}):
            response = self.client.get("/internal/whatsapp/readiness")
        self.assertEqual(response.status_code, 401)
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/internal/whatsapp/readiness",
                headers={"x-yamsi-key": "anything"})
        self.assertEqual(response.status_code, 503)

    def test_reports_dimensions_without_values(self):
        with patch.dict(os.environ, FULL_ENV, clear=True), \
             patch("provider_accounts.list_accounts",
                   new_callable=AsyncMock, return_value=[]):
            response = self.client.get("/internal/whatsapp/readiness",
                headers={"x-yamsi-key": OWNER_KEY})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ("ready", "production_mode", "dimensions",
                "enabled_provider_account_count", "missing", "malformed",
                "conflicting", "warnings", "remediation"):
            self.assertIn(key, body)
        for dimension in ("database", "provider", "webhook",
                "outbound_dispatch"):
            self.assertIn(dimension, body["dimensions"])
        for secret in SECRETS:
            self.assertNotIn(secret, _blob(body))

    def test_tenant_count_served_and_redacted(self):
        rows = [{"business_id": "water", "branch_id": "asaba",
            "provider": "whatsapp", "provider_account": "acct-1",
            "enabled": True, "label": "Main line"},
            {"business_id": "water", "branch_id": "north",
            "provider": "whatsapp", "provider_account": "acct-2",
            "enabled": False, "label": "Old line"}]
        with patch.dict(os.environ, FULL_ENV, clear=True), \
             patch("provider_accounts.list_accounts",
                   new_callable=AsyncMock, return_value=rows):
            response = self.client.get("/internal/whatsapp/readiness",
                headers={"x-yamsi-key": OWNER_KEY},
                params={"tenant_id": "tenant-1"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["enabled_provider_account_count"], 1)
        for secret in SECRETS:
            self.assertNotIn(secret, _blob(body))

    def test_registry_outage_still_returns_report(self):
        with patch.dict(os.environ, FULL_ENV, clear=True), \
             patch("provider_accounts.list_accounts",
                   new_callable=AsyncMock,
                   side_effect=DatabaseUnavailable("down")):
            response = self.client.get("/internal/whatsapp/readiness",
                headers={"x-yamsi-key": OWNER_KEY},
                params={"tenant_id": "tenant-1"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["enabled_provider_account_count"])
        self.assertTrue(body["warnings"])


# ---------------------------------------------------------------------------
# 4. Webhook production safety.
# ---------------------------------------------------------------------------

def _signed(raw, secret=SECRET):
    return "sha256=" + hmac.new(
        secret.encode(), raw, hashlib.sha256).hexdigest()


class WebhookProductionTests(unittest.TestCase):
    PAYLOAD = {"object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "acct-1"},
            "messages": [{"id": "m1", "from": "+12025550123",
                "type": "text", "text": {"body": "hi"}}]}}]}]}

    def setUp(self):
        self.client = TestClient(app)

    def test_valid_verification_answers_challenge(self):
        with patch.dict(os.environ,
                {"WHATSAPP_VERIFY_TOKEN": "verify-token-0001"}):
            response = self.client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-token-0001",
                "hub.challenge": "challenge-abc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "challenge-abc")

    def test_invalid_verification_denied_without_config_leak(self):
        with patch.dict(os.environ,
                {"WHATSAPP_VERIFY_TOKEN": "verify-token-0001"}):
            response = self.client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "challenge-abc"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("verify-token-0001", response.text)

    def test_unconfigured_verification_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": "anything",
                "hub.challenge": "challenge-abc"})
        self.assertEqual(response.status_code, 503)

    def test_valid_signature_accepted_in_any_mode(self):
        raw = json.dumps(self.PAYLOAD).encode()
        for mode in ("production", "local", "test"):
            with patch.dict(os.environ,
                    {"WHATSAPP_APP_SECRET": SECRET,
                     "WHATSAPP_ENV": mode}), \
                 patch("whatsapp_webhook.save_events",
                       new_callable=AsyncMock), \
                 patch("message_processor.process_inbox_batch",
                       new_callable=AsyncMock):
                response = self.client.post("/webhooks/whatsapp",
                    content=raw,
                    headers={"x-hub-signature-256": _signed(raw)})
            self.assertEqual(response.status_code, 200, msg=mode)
            self.assertTrue(response.json()["received"])

    def test_missing_signature_rejected_in_every_mode(self):
        raw = json.dumps(self.PAYLOAD).encode()
        # No local/test bypass exists: signatures are always required.
        for env in ({"WHATSAPP_APP_SECRET": SECRET},
                {"WHATSAPP_APP_SECRET": SECRET, "WHATSAPP_ENV": "local"},
                {"WHATSAPP_APP_SECRET": SECRET, "WHATSAPP_ENV": "test"}):
            with patch.dict(os.environ, env, clear=True):
                response = self.client.post("/webhooks/whatsapp",
                    content=raw)
            self.assertEqual(response.status_code, 403)

    def test_invalid_signature_rejected(self):
        raw = json.dumps(self.PAYLOAD).encode()
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": SECRET}):
            response = self.client.post("/webhooks/whatsapp",
                content=raw,
                headers={"x-hub-signature-256": _signed(b"tampered")})
        self.assertEqual(response.status_code, 403)

    def test_unconfigured_secret_fails_closed(self):
        raw = json.dumps(self.PAYLOAD).encode()
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/webhooks/whatsapp",
                content=raw,
                headers={"x-hub-signature-256": _signed(raw, "x")})
        self.assertEqual(response.status_code, 503)

    def test_duplicate_delivery_acknowledged_idempotently(self):
        raw = json.dumps(self.PAYLOAD).encode()
        sig = _signed(raw)
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": SECRET}), \
             patch("whatsapp_webhook.save_events",
                   new_callable=AsyncMock) as save, \
             patch("message_processor.process_inbox_batch",
                   new_callable=AsyncMock):
            first = self.client.post("/webhooks/whatsapp", content=raw,
                headers={"x-hub-signature-256": sig})
            second = self.client.post("/webhooks/whatsapp", content=raw,
                headers={"x-hub-signature-256": sig})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # The inbox insert carries the duplicate-tolerant upsert headers,
        # so redelivery can never create a second row.
        self.assertEqual(save.call_count, 2)

    def test_batch_duplicates_collapse_to_one_event(self):
        doubled = {"object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "acct-1"},
                "messages": [
                    {"id": "m1", "from": "s",
                     "type": "text", "text": {"body": "hi"}},
                    {"id": "m1", "from": "s",
                     "type": "text", "text": {"body": "hi"}}]}}]}]}
        rows = whatsapp_webhook.extract_events(doubled)
        self.assertEqual(len(rows), 1)

    def test_unsupported_payload_acknowledged_without_rows(self):
        payload = {"object": "something_else", "entry": []}
        raw = json.dumps(payload).encode()
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": SECRET}), \
             patch("whatsapp_webhook.save_events",
                   new_callable=AsyncMock) as save:
            response = self.client.post("/webhooks/whatsapp",
                content=raw,
                headers={"x-hub-signature-256": _signed(raw)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["events"], 0)
        save.assert_not_called()

    def test_ack_log_carries_no_secret_or_sender(self):
        raw = json.dumps(self.PAYLOAD).encode()
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": SECRET}), \
             patch("whatsapp_webhook.save_events",
                   new_callable=AsyncMock), \
             patch("message_processor.process_inbox_batch",
                   new_callable=AsyncMock), \
             self.assertLogs("yamsi", level="INFO") as logs:
            self.client.post("/webhooks/whatsapp", content=raw,
                headers={"x-hub-signature-256": _signed(raw)})
        blob = "\n".join(logs.output)
        self.assertIn("webhook_ack", blob)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("+12025550123", blob)


# ---------------------------------------------------------------------------
# 5. Outbound production safety.
# ---------------------------------------------------------------------------

class OutboundIsolationTests(unittest.TestCase):
    def _authorized(self, tenant, business, branch, rows):
        with patch("provider_accounts.rest_get", new_callable=AsyncMock,
                   return_value=rows):
            return asyncio.run(
                provider_accounts.is_account_authorized(
                    tenant, business, branch, "whatsapp", "acct-1"))

    def test_other_tenant_row_never_authorizes(self):
        # The registry query is tenant-scoped: another tenant's row is
        # simply not returned, so no fallback can authorize it here.
        self.assertFalse(self._authorized(
            "tenant-a", "water", "asaba", []))
        other = [{"tenant_id": "tenant-b", "business_id": "water",
            "branch_id": "asaba", "provider": "whatsapp",
            "provider_account": "acct-1", "enabled": True}]
        self.assertFalse(self._authorized(
            "tenant-a", "water", "asaba", other))

    def test_other_branch_row_never_authorizes(self):
        row = [{"tenant_id": "tenant-a", "business_id": "water",
            "branch_id": "warri", "provider": "whatsapp",
            "provider_account": "acct-1", "enabled": True}]
        self.assertFalse(self._authorized(
            "tenant-a", "water", "asaba", row))

    def test_exact_scope_authorizes(self):
        row = [{"tenant_id": "tenant-a", "business_id": "water",
            "branch_id": "asaba", "provider": "whatsapp",
            "provider_account": "acct-1", "enabled": True}]
        self.assertTrue(self._authorized(
            "tenant-a", "water", "asaba", row))


class OutboundClientSafetyTests(unittest.TestCase):
    def _send(self, response=None, side_effect=None, env=None):
        client = AsyncMock()
        if side_effect is not None:
            client.post = AsyncMock(side_effect=side_effect)
        else:
            client.post = AsyncMock(return_value=response)
        base = {"WHATSAPP_ACCESS_TOKEN": TOKEN}
        base.update(env or {})
        with patch.dict(os.environ, base, clear=True), \
             patch("outbound_whatsapp.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            try:
                return asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "+12025550123", "hello"))
            except outbound_whatsapp.OutboundUnavailable as error:
                return error

    def test_timeout_and_rate_limit_are_retryable(self):
        for effect in (httpx.TimeoutException("slow"),
                httpx.ConnectError("down")):
            error = self._send(side_effect=effect)
            self.assertIsInstance(error,
                outbound_whatsapp.RetryableOutboundError)
            self.assertTrue(error.retryable)
        for status in (429, 500, 503):
            error = self._send(
                response=httpx.Response(status, json={}))
            self.assertIsInstance(error,
                outbound_whatsapp.RetryableOutboundError, msg=status)
            self.assertTrue(error.retryable)

    def test_refusals_are_permanent_and_sanitized(self):
        error = self._send(response=httpx.Response(400, json={
            "error": {"code": 131030, "message": "Invalid recipient",
                "error_data": {"details": "must never leak"}}}))
        self.assertIsInstance(error,
            outbound_whatsapp.PermanentOutboundError)
        self.assertFalse(error.retryable)
        self.assertNotIn(TOKEN, str(error))
        self.assertNotIn("Invalid recipient", str(error))
        self.assertNotIn("must never leak", str(error))

    def test_missing_token_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("outbound_whatsapp.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = AsyncMock()
            with self.assertRaises(
                    outbound_whatsapp.OutboundNotConfigured):
                asyncio.run(outbound_whatsapp.send_text_message(
                    "acct-1", "+12025550123", "hello"))

    def test_timeout_override_validated(self):
        self.assertEqual(
            self._timeout_read({"OUTBOUND_HTTP_TIMEOUT_SECONDS": "30"}),
            30.0)
        self.assertEqual(self._timeout_read({}), 10.0)
        for bad in ("abc", "0", "999"):
            self.assertEqual(
                self._timeout_read(
                    {"OUTBOUND_HTTP_TIMEOUT_SECONDS": bad}), 10.0,
                msg=bad)

    def _timeout_read(self, env):
        with patch.dict(os.environ, env, clear=True):
            return outbound_whatsapp._timeout().read


class DuplicatePreventionTests(unittest.TestCase):
    ROW = {"id": "msg-1", "tenant_id": "tenant-1",
        "business_id": "water", "branch_id": "asaba", "status": "queued",
        "provider_sender": "+12025550123", "provider_account": "acct-1",
        "message_text": "hello"}

    def test_second_claim_no_ops_and_never_sends(self):
        with patch("outbound_dispatch_worker.rest_get",
                   new_callable=AsyncMock,
                   return_value=[dict(self.ROW)]), \
             patch("outbound_dispatch_worker.rest_patch",
                   new_callable=AsyncMock,
                   return_value=httpx.Response(200, json=[])), \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message",
                   new_callable=AsyncMock) as dispatch, \
             patch("outbound_dispatch_worker.retry_engine.record_success",
                   new_callable=AsyncMock), \
             patch("outbound_dispatch_worker.retry_engine.record_failure",
                   new_callable=AsyncMock):
            summary = asyncio.run(
                outbound_dispatch_worker.dispatch_pending())
        self.assertEqual(summary["claim_conflicts"], 1)
        self.assertEqual(summary["sent"], 0)
        dispatch.assert_not_called()

    def test_stale_claim_recovery_round_trip(self):
        body = {"status": "ok", "reclaimed": 1, "message_ids": ["msg-1"],
            "tenants": ["tenant-1"], "is_retry": False}
        client = AsyncMock()
        client.post = AsyncMock(
            return_value=httpx.Response(200, json=body))
        with patch("outbound_dispatch_worker.credentials",
                   return_value=("https://example.supabase.co", "k")), \
             patch("outbound_dispatch_worker.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            result = asyncio.run(
                outbound_dispatch_worker.recover_stale_claims(
                    stale_seconds=1800, limit=20))
        self.assertEqual(result["reclaimed"], 1)
        posted = client.post.call_args
        self.assertEqual(posted.args[0],
            "/rest/v1/rpc/amose_reclaim_stale_outbound")

    def test_recovery_rejects_bad_shapes(self):
        for bad in ("10", "999999", "abc", None):
            with self.assertRaises(
                    outbound_dispatch_worker.OutboundRecoveryError,
                    msg=str(bad)):
                asyncio.run(
                    outbound_dispatch_worker.recover_stale_claims(
                        stale_seconds=bad))


# ---------------------------------------------------------------------------
# 6. Masked logging.
# ---------------------------------------------------------------------------

class MaskedLoggingTests(unittest.TestCase):
    def test_mask_phone_keeps_only_last_four(self):
        self.assertEqual(safe_logging.mask_phone("+12025550123"),
            "+*******0123")
        self.assertEqual(safe_logging.mask_phone("2348012345678"),
            "*********5678")
        for hidden in ("123", "", "   ", None, 12345):
            self.assertEqual(safe_logging.mask_phone(hidden), "***")

    def test_secret_fields_redacted(self):
        line = safe_logging.format_event("evt", {
            "access_token": TOKEN, "to": "+12025550123",
            "message_id": "m1"})
        self.assertNotIn(TOKEN, line)
        self.assertIn("[REDACTED]", line)
        self.assertIn("+*******0123", line)
        self.assertNotIn("+12025550123", line)
        self.assertIn("message_id=m1", line)

    def test_dispatch_logs_masked_recipient(self):
        row = dict(DuplicatePreventionTests.ROW)
        with patch("outbound_dispatch_worker.rest_get",
                   new_callable=AsyncMock, return_value=[row]), \
             patch("outbound_dispatch_worker._claim",
                   new_callable=AsyncMock,
                   return_value={**row, "status": "sending"}), \
             patch("outbound_dispatch_worker.rest_patch",
                   new_callable=AsyncMock), \
             patch("outbound_dispatch_worker.notifier.dispatch_queued_message",
                   new_callable=AsyncMock,
                   return_value={"status": "sent"}), \
             patch("outbound_dispatch_worker.retry_engine.record_success",
                   new_callable=AsyncMock), \
             patch("outbound_dispatch_worker.retry_engine.record_failure",
                   new_callable=AsyncMock), \
             self.assertLogs("yamsi", level="INFO") as logs:
            asyncio.run(outbound_dispatch_worker.dispatch_pending())
        blob = "\n".join(logs.output)
        self.assertIn("outbound_sent", blob)
        self.assertIn("+*******0123", blob)
        self.assertNotIn("+12025550123", blob)


# ---------------------------------------------------------------------------
# 7. Local/test mode stays usable; production stays fail-closed.
# ---------------------------------------------------------------------------

class ModeBehaviorTests(unittest.TestCase):
    def test_local_mode_needs_no_real_credentials(self):
        env = {"WHATSAPP_ENV": "local"}
        with patch.dict(os.environ, env, clear=True):
            result = production_readiness.validate_environment()
        self.assertFalse(result["production_mode"])
        self.assertFalse(result["ready"])
        # Report, not exception: local development stays usable.
        self.assertTrue(result["missing"])

    def test_production_defaults_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            result = production_readiness.validate_environment()
        self.assertTrue(result["production_mode"])
        self.assertFalse(result["ready"])

    def test_sending_without_token_fails_closed_in_any_mode(self):
        for mode in ("production", "local", "test"):
            with patch.dict(os.environ, {"WHATSAPP_ENV": mode},
                    clear=True), \
                 patch("outbound_whatsapp.httpx.AsyncClient"):
                with self.assertRaises(
                        outbound_whatsapp.OutboundNotConfigured,
                        msg=mode):
                    asyncio.run(outbound_whatsapp.send_text_message(
                        "acct-1", "+12025550123", "hello"))


if __name__ == "__main__":
    unittest.main()
