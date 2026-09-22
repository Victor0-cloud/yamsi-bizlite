"""Telegram review-backup channel tests.

Covers: webhook authentication (missing/wrong secret rejected before body
processing), request-size and JSON limits, transactional duplicate-update
handling, secure linking (one-time, expiring, untrusted identifiers
rejected), tamper-resistant/idempotent/replay-safe callbacks, cross-tenant
and unauthorized-actor refusal, retry/rollback behavior, WhatsApp
independence, and the no-secret-leak / no-direct-write guarantees.

Every database and Telegram API interaction is mocked: no test contacts a
real service or reveals a real secret.
"""
import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import human_confirmation
import outbound_telegram
import outbound_telegram as outbound_telegram_module
import review_service as review_service_module
import telegram_adapter as tg
import telegram_webhook
from app import app

SECRET = "test-telegram-secret"
BOT_TOKEN = "test-bot-token"
CHAT_ID = "123456789"
REF = "YR-ABCD234EFG"
SUBMISSION_UUID = "11111111-1111-1111-1111-111111111111"
TENANT_UUID = "00000000-0000-0000-0000-000000000001"
EMP_UUID = "22222222-2222-2222-2222-222222222222"


def message_update(update_id=100, text="hello", chat_id=CHAT_ID,
        sender_id=None):
    sender = int(sender_id if sender_id is not None else chat_id)
    return {"update_id": update_id,
        "message": {"message_id": 1,
            "from": {"id": sender, "first_name": "Attacker Chosen Name",
                "username": "spoofed_name"},
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text}}


def callback_update(update_id=101, token="a" * 32, chat_id=CHAT_ID):
    return {"update_id": update_id,
        "callback_query": {"id": "cq-1",
            "from": {"id": int(chat_id)},
            "message": {"message_id": 7, "chat": {"id": int(chat_id)}},
            "data": token}}


def mock_batch_client():
    client = AsyncMock()
    client.patch = AsyncMock(return_value=httpx.Response(204))
    client.get = AsyncMock(return_value=httpx.Response(200, json=[]))
    client.post = AsyncMock(return_value=httpx.Response(200, json={}))
    return client


class WebhookAuthTests(unittest.IsolatedAsyncioTestCase):
    # Uses httpx ASGI transport (no sockets): the sandbox forbids the
    # listening sockets starlette TestClient needs, so HTTP-level tests
    # run in-process. Real-network behavior is identical -- same ASGI app.
    async def post(self, body, secret=SECRET):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                base_url="http://test") as client:
            headers = {}
            if secret is not None:
                headers["x-telegram-bot-api-secret-token"] = secret
            return await client.post("/webhooks/telegram", content=body,
                headers=headers)

    async def test_missing_secret_config_locks_endpoint(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": ""}):
            response = await self.post(json.dumps(message_update()).encode())
        self.assertEqual(response.status_code, 503)

    async def test_missing_header_rejected(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock) as save:
            response = await self.post(
                json.dumps(message_update()).encode(), secret=None)
        self.assertEqual(response.status_code, 403)
        save.assert_not_called()

    async def test_wrong_secret_rejected_before_body_processing(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock) as save:
            # Body is deliberately malformed: a 403 (not 400) proves the
            # secret gate runs before any body processing.
            response = await self.post(b"{not json", secret="wrong-secret")
        self.assertEqual(response.status_code, 403)
        save.assert_not_called()

    async def test_malformed_body_with_valid_secret_is_400(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock) as save:
            response = await self.post(b"{not json")
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    async def test_non_update_object_rejected(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock) as save:
            response = await self.post(json.dumps({"foo": "bar"}).encode())
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    async def test_missing_update_id_rejected(self):
        update = message_update()
        del update["update_id"]
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock) as save:
            response = await self.post(json.dumps(update).encode())
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    async def test_unsupported_update_kind_rejected(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock) as save:
            response = await self.post(
                json.dumps({"update_id": 5, "edited_message": {}}).encode())
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    async def test_oversized_rejected(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}):
            response = await self.post(b"x" * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)

    async def test_valid_update_stored_then_processed_in_background(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock, return_value="inbox-1") as save, \
             patch.object(telegram_webhook, "_trigger_processing",
                new_callable=AsyncMock) as trigger:
            response = await self.post(json.dumps(message_update()).encode())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True})
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], 100)
        trigger.assert_called_once()

    async def test_duplicate_update_acknowledged_without_reprocessing(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock, return_value=None) as save, \
             patch.object(telegram_webhook, "_trigger_processing",
                new_callable=AsyncMock) as trigger:
            response = await self.post(json.dumps(message_update()).encode())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
            {"received": True, "duplicate": True})
        save.assert_called_once()
        trigger.assert_not_called()

    async def test_inbox_failure_requests_retry(self):
        from fastapi import HTTPException
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock,
                side_effect=HTTPException(503, "retry")):
            response = await self.post(json.dumps(message_update()).encode())
        self.assertEqual(response.status_code, 503)

    async def test_responses_never_carry_secrets(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": SECRET}), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock, return_value="inbox-1"), \
             patch.object(telegram_webhook, "_trigger_processing",
                new_callable=AsyncMock):
            for secret in (None, "wrong-secret", SECRET):
                response = await self.post(
                    json.dumps(message_update()).encode(), secret=secret)
                self.assertNotIn(SECRET, response.text)

    async def test_works_without_whatsapp_credentials(self):
        env = {key: "" for key in ("WHATSAPP_VERIFY_TOKEN",
            "WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN")}
        env["TELEGRAM_WEBHOOK_SECRET"] = SECRET
        with patch.dict(os.environ, env), \
             patch.object(telegram_webhook, "save_update",
                new_callable=AsyncMock, return_value="inbox-1"), \
             patch.object(telegram_webhook, "_trigger_processing",
                new_callable=AsyncMock):
            response = await self.post(json.dumps(message_update()).encode())
        self.assertEqual(response.status_code, 200)


class SaveUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, status, body):
        client = AsyncMock()
        client.post = AsyncMock(
            return_value=httpx.Response(status, json=body))
        return client

    async def test_insert_uses_transactional_dedup_key(self):
        client = await self._client(201, [{"id": "inbox-9"}])
        with patch("telegram_webhook.credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch("telegram_webhook.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            inbox_id = await telegram_webhook.save_update(4242,
                message_update(update_id=4242))
        self.assertEqual(inbox_id, "inbox-9")
        kwargs = client.post.call_args.kwargs
        sent = kwargs["json"][0]
        self.assertEqual(sent["provider"], "telegram")
        self.assertEqual(sent["provider_account"], tg.BOT_ACCOUNT)
        self.assertEqual(sent["provider_event_id"], "update:4242")
        self.assertIn("ignore-duplicates", kwargs["headers"]["Prefer"])
        self.assertEqual(kwargs["params"]["on_conflict"],
            "provider,provider_account,provider_event_id")

    async def test_conflict_returns_none_duplicate(self):
        client = await self._client(200, [])
        with patch("telegram_webhook.credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch("telegram_webhook.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertIsNone(await telegram_webhook.save_update(4242, {}))

    async def test_database_denial_raises_503(self):
        from fastapi import HTTPException
        client = await self._client(403, {})
        with patch("telegram_webhook.credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch("telegram_webhook.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(HTTPException) as ctx:
                await telegram_webhook.save_update(1, {})
        self.assertEqual(ctx.exception.status_code, 503)


class ParseTests(unittest.TestCase):
    def test_display_names_never_become_identity(self):
        update = tg.parse_update(message_update(text="/start"))
        sender = tg.message_sender(update["message"])
        self.assertEqual(sender, CHAT_ID)
        self.assertNotIn("spoofed_name", sender)
        self.assertNotIn("Attacker", sender)

    def test_username_sender_rejected(self):
        with self.assertRaises(tg.TelegramAdapterError):
            tg._require_sender_id("spoofed_name")

    def test_phone_sender_rejected(self):
        with self.assertRaises(tg.TelegramAdapterError):
            tg._require_sender_id("+12025550123")

    def test_bool_update_id_rejected(self):
        with self.assertRaises(tg.TelegramAdapterError):
            tg.parse_update({"update_id": True, "message": {}})

    def test_start_token_split(self):
        self.assertIsNone(tg.split_start_token("/start"))
        self.assertEqual(tg.split_start_token("/start abcDEF123-_xyz019"),
            "abcDEF123-_xyz019")
        self.assertEqual(
            tg.split_start_token("/start@YamsiBizLiteBot abcDEF123-_xyz019"),
            "abcDEF123-_xyz019")
        self.assertIsNone(tg.split_start_token("REVIEW CONFIRM X KEY Y"))
        with self.assertRaises(tg.TelegramAdapterError):
            tg.split_start_token("/start ***")

    def test_is_start_command(self):
        self.assertTrue(tg.is_start_command("/start"))
        self.assertTrue(tg.is_start_command("/START tok"))
        self.assertTrue(tg.is_start_command("/start@YamsiBizLiteBot tok"))
        self.assertFalse(tg.is_start_command("REVIEW CONFIRM X KEY Y"))
        self.assertFalse(tg.is_start_command(""))
        self.assertFalse(tg.is_start_command(None))

    def test_callback_parts_reject_forged_shapes(self):
        query_id, sender, chat_id, message_id, token = tg.callback_parts(
            callback_update()["callback_query"])
        self.assertEqual((sender, chat_id, message_id), (CHAT_ID, CHAT_ID, 7))
        bad = callback_update()
        bad["callback_query"]["data"] = "approve:" + REF + ":evil"
        with self.assertRaises(tg.TelegramAdapterError):
            tg.callback_parts(bad["callback_query"])
        bad2 = callback_update()
        bad2["callback_query"]["from"] = {"username": "spoof"}
        with self.assertRaises(tg.TelegramAdapterError):
            tg.callback_parts(bad2["callback_query"])

    def test_callback_data_fits_telegram_64_byte_cap(self):
        token = tg.new_callback_token()
        self.assertLessEqual(len(token), 64)
        keyboard = tg.review_keyboard(token, token)
        for row in keyboard["inline_keyboard"]:
            for button in row:
                self.assertLessEqual(len(button["callback_data"]), 64)

    def test_tokens_are_high_entropy_and_hashed(self):
        seen = {tg.new_link_token() for _ in range(50)}
        self.assertEqual(len(seen), 50)
        token = tg.new_link_token()
        digest = tg.sha256_hex(token)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn(token, digest)

    def test_outbound_ref_parser_fails_closed(self):
        key = "tg_review_req:%s:%s:%s" % (TENANT_UUID, REF, EMP_UUID)
        self.assertEqual(tg._parse_outbound_ref(key), REF)
        for bad in (None, "", "review_req:x", "tg_review_req:a:b",
                "tg_review_req:%s:NOT-A-REF:%s" % (TENANT_UUID, EMP_UUID),
                "whatsapp:" + REF):
            self.assertIsNone(tg._parse_outbound_ref(bad))


class LinkRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_issue_returns_single_use_url_and_stores_only_hash(self):
        captured = {}

        async def fake_rpc(function_name, allowed, payload):
            captured["payload"] = payload
            return {"status": "issued", "tenant_id": TENANT_UUID,
                "employee_id": EMP_UUID, "request_key": "link-1",
                "expires_at": "2030-01-01T00:00:00+00:00", "is_retry": False}

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc):
            result = await tg.issue_employee_link(
                TENANT_UUID, EMP_UUID, "link-1")
        self.assertTrue(result["link_url"].startswith("https://t.me/"))
        token = result["link_url"].split("start=")[1]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{16,64}$")
        # Only the hash crosses into the RPC payload -- never plaintext.
        self.assertEqual(captured["payload"]["p_token_hash"],
            hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(captured["payload"]))

    async def test_issue_rejects_bad_scope_before_rpc(self):
        with patch.object(tg, "_call_rpc",
                new_callable=AsyncMock) as rpc:
            with self.assertRaises(tg.TelegramAdapterError):
                await tg.issue_employee_link("not-a-uuid", EMP_UUID, "k")
            with self.assertRaises(tg.TelegramAdapterError):
                await tg.issue_employee_link(TENANT_UUID, EMP_UUID, "bad key!")
            rpc.assert_not_called()

    async def test_issue_refusal_maps_to_link_error(self):
        async def fake_rpc(function_name, allowed, payload):
            raise human_confirmation.SubmissionNotFoundError(
                "NOT_FOUND: employee is not known")

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc):
            with self.assertRaises(tg.TelegramLinkError):
                await tg.issue_employee_link(TENANT_UUID, EMP_UUID, "link-1")

    async def test_consume_rejects_untrusted_sender_before_rpc(self):
        with patch.object(tg, "_call_rpc",
                new_callable=AsyncMock) as rpc:
            for bad_sender in ("spoofed_name", "+12025550123", "", None):
                with self.assertRaises(tg.TelegramAdapterError):
                    await tg.consume_link_token("a" * 32, bad_sender)
            rpc.assert_not_called()

    async def test_consume_used_or_expired_links_share_one_refusal(self):
        async def fake_rpc(function_name, allowed, payload):
            raise human_confirmation.SubmissionNotFoundError(
                "NOT_FOUND: link is not valid")

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc):
            with self.assertRaises(tg.TelegramLinkError):
                await tg.consume_link_token("b" * 32, CHAT_ID)

    async def test_consume_bound_to_other_employee_refused(self):
        async def fake_rpc(function_name, allowed, payload):
            raise human_confirmation.RequestConflictError(
                "CONFLICT: sender is already linked to a different employee")

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc):
            with self.assertRaises(tg.TelegramLinkError):
                await tg.consume_link_token("c" * 32, CHAT_ID)


class CallbackRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_mint_validates_locally_before_rpc(self):
        with patch.object(tg, "_call_rpc",
                new_callable=AsyncMock) as rpc:
            # A malformed reference raises the shared boundary error
            # (same precedent as review_service), never reaching the RPC.
            with self.assertRaises(
                    human_confirmation.ReviewReferenceError):
                await tg.mint_button_token("NOT-A-REF", "approve", CHAT_ID)
            with self.assertRaises(tg.TelegramAdapterError):
                await tg.mint_button_token(REF, "delete", CHAT_ID)
            with self.assertRaises(tg.TelegramAdapterError):
                await tg.mint_button_token(REF, "approve", "spoofed")
            rpc.assert_not_called()

    async def test_mint_unauthorized_reviewer_refused(self):
        async def fake_rpc(function_name, allowed, payload):
            raise human_confirmation.ReviewUnauthorizedError(
                "UNAUTHORIZED: reviewer could not be authorized")

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc):
            with self.assertRaises(tg.TelegramCallbackError):
                await tg.mint_button_token(REF, "approve", CHAT_ID)

    async def test_consume_forged_token_refused_without_review_call(self):
        async def fake_rpc(function_name, allowed, payload):
            raise human_confirmation.SubmissionNotFoundError(
                "NOT_FOUND: callback is not known")

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc), \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock) as confirm:
            with self.assertRaises(tg.TelegramCallbackError):
                await tg.consume_button_token("z" * 32, CHAT_ID)
            confirm.assert_not_called()

    async def test_consume_rejects_malformed_token_before_rpc(self):
        with patch.object(tg, "_call_rpc",
                new_callable=AsyncMock) as rpc:
            with self.assertRaises(tg.TelegramCallbackError):
                await tg.consume_button_token("approve:" + REF, CHAT_ID)
            rpc.assert_not_called()

    async def test_cross_tenant_press_refused(self):
        async def fake_rpc(function_name, allowed, payload):
            raise human_confirmation.ReviewUnauthorizedError(
                "UNAUTHORIZED: reviewer could not be authorized")

        with patch.object(tg, "_call_rpc", side_effect=fake_rpc):
            with self.assertRaises(tg.TelegramCallbackError):
                await tg.consume_button_token("d" * 32, "999888777")


class ProcessMessageTests(unittest.IsolatedAsyncioTestCase):
    async def _run_message(self, text, consume=None, command=None,
            identity_rows=(), identity_failure=None, chat_id=CHAT_ID,
            sender_id=None):
        client = mock_batch_client()
        if identity_failure == "transport":
            client.get = AsyncMock(side_effect=httpx.HTTPError("db down"))
        elif identity_failure == "status":
            client.get = AsyncMock(
                return_value=httpx.Response(500, json={"message": "down"}))
        else:
            client.get = AsyncMock(return_value=httpx.Response(
                200, json=list(identity_rows)))
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text=text, chat_id=chat_id, sender_id=sender_id))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "consume_link_token",
                new_callable=AsyncMock) as consume_mock, \
             patch.object(review_service_module, "handle_review_command",
                new_callable=AsyncMock) as command_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            if consume is not None:
                consume_mock.side_effect = consume
            if command is not None:
                command_mock.side_effect = command
            outcome = await tg._process_message(
                client, "inbox-1", update["message"], summary)
        return outcome, summary, client, consume_mock, command_mock, send_mock

    async def test_start_without_token_sends_help_and_processes(self):
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("/start")
        self.assertEqual(outcome["outcome"], "help")
        send_mock.assert_called_once()
        sent_text = send_mock.call_args.args[1]
        self.assertNotIn("start=", sent_text)
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})

    async def test_start_with_valid_token_links(self):
        async def ok(token, sender):
            return {"status": "linked"}

        outcome, summary, _, consume_mock, _, _ = \
            await self._run_message("/start Qtok1234567890-_",
                consume=ok)
        self.assertEqual(outcome["outcome"], "linked")
        self.assertEqual(summary["links_consumed"], 1)
        consume_mock.assert_called_once_with("Qtok1234567890-_", CHAT_ID)

    async def test_start_with_used_token_refused_generically(self):
        async def used(token, sender):
            raise tg.TelegramLinkError("NOT_FOUND: link is not valid")

        outcome, summary, client, _, _, send_mock = \
            await self._run_message("/start Qtok1234567890-_", consume=used)
        self.assertEqual(outcome["outcome"], "refused")
        sent_text = send_mock.call_args.args[1]
        self.assertIn("not valid", sent_text)
        self.assertNotIn("Qtok1234567890", sent_text)
        self.assertEqual(summary["reviews_refused"], 1)

    async def test_start_with_db_outage_marks_failed_for_retry(self):
        async def down(token, sender):
            raise human_confirmation.WorkflowDatabaseError("db down")

        outcome, summary, client, _, _, _ = \
            await self._run_message("/start Qtok1234567890-_", consume=down)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(client.patch.call_args.kwargs["json"]["status"],
            "failed")

    async def test_review_confirm_text_uses_telegram_boundary(self):
        seen = {}

        async def fake_command(client, row, sender, command, summary):
            seen.update(row=row, sender=sender, command=command)
            summary["reviews_confirmed"] += 1
            return {"outcome": "confirm", "result": {}}

        text = "REVIEW CONFIRM %s KEY k1" % REF
        outcome, summary, _, _, command_mock, send_mock = \
            await self._run_message(text, command=fake_command)
        self.assertEqual(outcome["outcome"], "confirm")
        self.assertEqual(seen["row"], {"id": "inbox-1", "provider": "telegram"})
        self.assertEqual(seen["sender"], CHAT_ID)
        reply = send_mock.call_args.args[1]
        self.assertEqual("Approved \u2705", reply)
        self.assertNotIn(REF, reply)
        self.assertNotIn("Sold", reply)

    async def test_malformed_reserved_prefix_refused_without_writes(self):
        text = "REVIEW CONFIRM not-a-reference KEY k1"
        with patch.object(review_service_module,
                "refuse_malformed_command",
                new_callable=AsyncMock) as refuse:
            outcome, summary, _, _, _, send_mock = \
                await self._run_message(text)
            refuse.assert_called_once()
        self.assertEqual(outcome["outcome"], "refused")
        send_mock.assert_called_once()

    async def test_ordinary_text_never_creates_submissions(self):
        outcome, summary, client, _, command_mock, _ = \
            await self._run_message("Sold 50 bags at 500 NGN")
        command_mock.assert_not_called()
        self.assertEqual(
            [c for c in client.post.call_args_list
                if c.args[0] == "/rest/v1/biz_submissions"], [])
        self.assertEqual(summary["unmatched"], 1)

    async def test_ordinary_text_linked_sender_gets_intake_help(self):
        outcome, summary, client, _, command_mock, send_mock = \
            await self._run_message("hello",
                identity_rows=[{"tenant_id": TENANT_UUID,
                    "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
        command_mock.assert_not_called()
        self.assertEqual(
            [c for c in client.post.call_args_list
                if c.args[0] == "/rest/v1/biz_submissions"], [])
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertIn("one report per", reply.lower())
        self.assertIn("SALE", reply)
        self.assertIn("SALE 1 bag for 450 naira cash", reply)
        self.assertNotIn("REVIEW", reply)
        self.assertNotIn("not linked", reply.lower())
        identity_params = client.get.call_args_list[0].kwargs["params"]
        self.assertEqual(identity_params, {"provider": "eq.telegram",
            "provider_sender": "eq." + CHAT_ID,
            "select": "tenant_id,employee_id", "limit": "1"})
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})
        self.assertEqual(summary["unmatched"], 1)

    async def test_ordinary_text_unlinked_sender_keeps_linking_help(self):
        outcome, summary, _, _, command_mock, send_mock = \
            await self._run_message("hello")
        command_mock.assert_not_called()
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertIn("not linked", reply.lower())
        self.assertEqual(summary["unmatched"], 1)

    async def test_identity_uses_user_id_not_chat_id(self):
        # Linked from.id with a different chat.id: still recognized as
        # linked (identity binds the user id at link time), while the
        # reply is routed to the chat the message came from.
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("hello",
                identity_rows=[{"tenant_id": TENANT_UUID,
                    "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}],
                chat_id="999888777", sender_id=CHAT_ID)
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertNotIn("not linked", reply.lower())
        identity_params = client.get.call_args_list[0].kwargs["params"]
        self.assertEqual(
            identity_params["provider_sender"], "eq." + CHAT_ID)
        self.assertEqual(send_mock.call_args.args[0], "999888777")
        self.assertEqual(summary["unmatched"], 1)

    async def test_chat_id_alone_never_links_unlinked_sender(self):
        # Unlinked from.id with an unrelated chat.id: the chat id is
        # never consulted for identity, so the sender stays unlinked.
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("hello", chat_id="999888777",
                sender_id="111222333")
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertIn("not linked", reply.lower())
        params = client.get.call_args.kwargs["params"]
        self.assertEqual(params["provider_sender"], "eq.111222333")
        self.assertEqual(summary["unmatched"], 1)

    async def test_help_reply_leaks_no_cross_tenant_identifiers(self):
        # Whatever tenant the linked row belongs to, the help reply
        # exposes no tenant/employee/sender identifiers and the identity
        # lookup carries no tenant scope that could reach another
        # tenant's data (later assignment scoping reuses only the
        # locked row's own tenant).
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("hello",
                identity_rows=[{"tenant_id": TENANT_UUID,
                    "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertNotIn(TENANT_UUID, reply)
        self.assertNotIn(EMP_UUID, reply)
        self.assertNotIn(CHAT_ID, reply)
        identity_params = client.get.call_args_list[0].kwargs["params"]
        self.assertNotIn("tenant", " ".join(identity_params.keys()))

    async def test_ordinary_text_identity_lookup_failure_fails_closed(self):
        client = mock_batch_client()
        client.get = AsyncMock(side_effect=httpx.HTTPError("db down"))
        summary = tg._new_summary()
        update = tg.parse_update(message_update(text="hello"))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-7", summary)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["error"], "WorkflowDatabaseError")
        self.assertEqual(summary["failed"], 1)
        send_mock.assert_not_called()
        note = client.patch.call_args.kwargs["json"]
        self.assertEqual(note["status"], "failed")
        self.assertNotIn(CHAT_ID, note.get("processing_error", ""))
        self.assertNotIn("7551230024", note.get("processing_error", ""))

    async def test_start_without_token_sends_connected_when_linked(self):
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("/start", identity_rows=[
                {"tenant_id": TENANT_UUID, "employee_id": EMP_UUID}])
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertIn("already connected", reply)
        self.assertNotIn("not linked", reply.lower())

    async def test_start_without_token_asks_link_when_unlinked(self):
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("/start")
        self.assertEqual(outcome["outcome"], "help")
        reply = send_mock.call_args.args[1]
        self.assertIn("not linked", reply.lower())
        params = client.get.call_args.kwargs["params"]
        self.assertEqual(params["provider_sender"], "eq." + CHAT_ID)
        self.assertNotIn("tenant", " ".join(params.keys()))

    async def test_review_command_path_never_queries_sender_identities(self):
        async def fake_command(client, row, sender, command, summary):
            summary["reviews_confirmed"] += 1
            return {"outcome": "confirm", "result": {}}

        text = "REVIEW CONFIRM %s KEY k1" % REF
        outcome, summary, client, _, _, send_mock = \
            await self._run_message(text, command=fake_command)
        paths = [call.args[0] for call in client.get.call_args_list]
        self.assertNotIn("/rest/v1/biz_sender_identities", paths)
        self.assertEqual(outcome["outcome"], "confirm")
        reply = send_mock.call_args.args[1]
        self.assertIn("Approved", reply)
        self.assertNotIn(REF, reply)

    async def test_start_invalid_shape_token_refused_not_stuck(self):
        outcome, summary, client, _, _, send_mock = \
            await self._run_message("/start ***!!!")
        self.assertEqual(outcome["outcome"], "refused")
        send_mock.assert_called_once()
        self.assertNotIn("***", send_mock.call_args.args[1])
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})
        self.assertEqual(summary["reviews_refused"], 1)

    async def test_senderless_message_consumed_not_stuck(self):
        client = mock_batch_client()
        summary = tg._new_summary()
        update = {"update_id": 4242,
            "message": {"message_id": 1, "chat": {"id": 1}, "text": "hi"}}
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        self.assertEqual(outcome["outcome"], "refused")
        self.assertEqual(
            client.patch.call_args.kwargs["json"]["status"], "processed")
        self.assertEqual(summary["reviews_refused"], 1)


class ProcessCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def _run_callback(self, token="e" * 32, consume=None,
            confirm=None, flow_consume=None, stage=None):
        client = mock_batch_client()
        summary = tg._new_summary()
        update = tg.parse_update(callback_update(token=token))

        async def no_flow(token, sender):
            raise tg.TelegramCallbackError(
                "NOT_FOUND: flow button is not known")

        async def no_stage(*args, **kwargs):
            raise tg.TelegramLinkError("NOT_FOUND: no pending guided step")

        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "consume_button_token",
                new_callable=AsyncMock) as consume_mock, \
             patch.object(tg, "consume_flow_token",
                new_callable=AsyncMock) as flow_mock, \
             patch.object(tg, "stage_flow",
                new_callable=AsyncMock) as stage_mock, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock) as confirm_mock, \
             patch.object(outbound_telegram_module, "answer_callback_query",
                new_callable=AsyncMock) as answer_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock, \
             patch.object(outbound_telegram_module, "clear_inline_keyboard",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            if consume is not None:
                consume_mock.side_effect = consume
            if confirm is not None:
                confirm_mock.side_effect = confirm
            flow_mock.side_effect = flow_consume or no_flow
            stage_mock.side_effect = stage or no_stage
            outcome = await tg._process_callback(
                client, "inbox-2", update["callback_query"], summary)
        return outcome, summary, client, consume_mock, confirm_mock, \
            answer_mock, send_mock

    async def test_approve_ready_runs_chat_confirm_once(self):
        async def ready(token, sender):
            return {"status": "ready", "review_ref": REF, "action": "approve",
                "request_key": "tgcb-" + token, "is_retry": False}

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None):
            return {"status": "confirmed"}

        outcome, summary, client, _, confirm_mock, _, _ = \
            await self._run_callback(consume=ready, confirm=confirmed)
        self.assertEqual(outcome["outcome"], "confirm")
        self.assertEqual(summary["reviews_confirmed"], 1)
        confirm_mock.assert_called_once()
        args = confirm_mock.call_args.args
        self.assertEqual(args[1:5], (REF, "telegram", CHAT_ID, "tgcb-" + "e" * 32))
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})

    async def test_replay_reruns_same_idempotent_key_without_duplicates(self):
        calls = []

        async def already_used(token, sender):
            return {"status": "already_used", "review_ref": REF,
                "action": "approve", "request_key": "tgcb-" + token,
                "is_retry": True}

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None):
            calls.append(key)
            return {"status": "confirmed"}

        outcome, summary, _, _, confirm_mock, _, _ = \
            await self._run_callback(consume=already_used, confirm=confirmed)
        self.assertEqual(outcome["outcome"], "confirm")
        self.assertEqual(calls, ["tgcb-" + "e" * 32])
        confirm_mock.assert_called_once()

    async def test_forged_callback_fails_closed(self):
        async def forged(token, sender):
            raise tg.TelegramCallbackError("NOT_FOUND: callback is not known")

        outcome, summary, client, _, confirm_mock, answer_mock, _ = \
            await self._run_callback(consume=forged)
        self.assertEqual(outcome["outcome"], "refused")
        confirm_mock.assert_not_called()
        answer_mock.assert_called_once()
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(
            client.patch.call_args.kwargs["json"]["status"], "processed")

    async def test_cross_tenant_press_refused(self):
        async def wrong_binding(token, sender):
            raise tg.TelegramCallbackError(
                "UNAUTHORIZED: reviewer could not be authorized")

        outcome, summary, _, _, confirm_mock, _, _ = \
            await self._run_callback(token="f" * 32, consume=wrong_binding)
        self.assertEqual(outcome["outcome"], "refused")
        confirm_mock.assert_not_called()

    async def test_decided_case_reports_without_review_call(self):
        async def closed(token, sender):
            return {"status": "case_closed", "review_ref": REF,
                "action": "approve", "request_key": "tgcb-" + token,
                "is_retry": True}

        outcome, summary, _, _, confirm_mock, _, _ = \
            await self._run_callback(consume=closed)
        self.assertEqual(outcome["outcome"], "refused")
        confirm_mock.assert_not_called()
        self.assertEqual(summary["reviews_refused"], 1)

    async def test_expired_button_reports_without_review_call(self):
        async def expired(token, sender):
            return {"status": "expired", "review_ref": REF,
                "action": "approve", "request_key": "tgcb-" + token,
                "is_retry": False}

        outcome, summary, _, _, confirm_mock, _, _ = \
            await self._run_callback(consume=expired)
        self.assertEqual(outcome["outcome"], "refused")
        confirm_mock.assert_not_called()

    async def test_reject_button_starts_reason_flow_never_rejects(self):
        async def ready_reject(token, sender):
            return {"status": "ready", "review_ref": REF, "action": "reject",
                "request_key": "tgcb-" + token, "is_retry": False}

        async def staged(sender, **kwargs):
            return {"status": "staged"}

        with patch.object(review_service_module, "reject_from_chat",
                new_callable=AsyncMock) as reject_mock:
            outcome, summary, _, _, _, _, send_mock = \
                await self._run_callback(consume=ready_reject,
                    stage=staged)
            reject_mock.assert_not_called()
        self.assertEqual(outcome["outcome"], "reject_reason_requested")
        reply = send_mock.call_args.args[1]
        self.assertIn("short reason", reply)
        self.assertNotIn(REF, reply)
        self.assertNotIn("KEY", reply)

    async def test_unauthorized_actor_refused(self):
        async def ready(token, sender):
            return {"status": "ready", "review_ref": REF, "action": "approve",
                "request_key": "tgcb-" + token, "is_retry": False}

        async def denied(client, ref, provider, sender, key,
                correction_reason=None):
            raise human_confirmation.ReviewUnauthorizedError(
                "UNAUTHORIZED: reviewer could not be authorized")

        outcome, summary, client, _, confirm_mock, _, _ = \
            await self._run_callback(consume=ready, confirm=denied)
        self.assertEqual(outcome["outcome"], "refused")
        confirm_mock.assert_called_once()
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(
            client.patch.call_args.kwargs["json"]["status"], "processed")

    async def test_confirm_retry_marks_failed_for_manual_reprocessing(self):
        async def ready(token, sender):
            return {"status": "ready", "review_ref": REF, "action": "approve",
                "request_key": "tgcb-" + token, "is_retry": False}

        async def down(client, ref, provider, sender, key,
                correction_reason=None):
            raise human_confirmation.WorkflowDatabaseError("db down")

        outcome, summary, client, _, _, _, _ = \
            await self._run_callback(consume=ready, confirm=down)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(client.patch.call_args.kwargs["json"]["status"],
            "failed")


def queued_row(row_id="out-1", status="queued", message_type="review_request",
        key=None):
    return {"id": row_id, "tenant_id": TENANT_UUID,
        "provider_sender": CHAT_ID, "recipient_employee_id": EMP_UUID,
        "message_type": message_type,
        "message_text": "Review request %s" % REF,
        "idempotency_key": key or "tg_review_req:%s:%s:%s"
            % (TENANT_UUID, REF, EMP_UUID),
        "status": status}


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def _run_dispatch(self, rows, mint=None, send=None,
            review_ref=None, flow_mint=None):
        client = mock_batch_client()

        async def fake_get(path, params=None):
            return httpx.Response(200, json=rows)

        async def fake_patch(path, params=None, json=None, headers=None):
            target = [r for r in rows if r["id"] == params["id"][3:]]
            if params.get("status") == "eq.queued" and target \
                    and target[0]["status"] == "queued":
                target[0]["status"] = json["status"]
                return httpx.Response(200, json=[target[0]])
            return httpx.Response(200, json=[])

        async def no_flow_mint(*args, **kwargs):
            raise tg.TelegramLinkError("NOT_FOUND: flow button is not known")

        client.get = AsyncMock(side_effect=fake_get)
        client.patch = AsyncMock(side_effect=fake_patch)
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "mint_button_token",
                new_callable=AsyncMock) as mint_mock, \
             patch.object(tg, "mint_flow_token",
                new_callable=AsyncMock) as flow_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            if mint is not None:
                mint_mock.side_effect = mint
            else:
                async def ok_mint(ref, action, sender):
                    return {"token": "tok-%s" % action,
                        "request_key": "tgcb-tok-%s" % action}
                mint_mock.side_effect = ok_mint
            flow_mock.side_effect = flow_mint or no_flow_mint
            if send is not None:
                send_mock.side_effect = send
            else:
                send_mock.return_value = {"provider_message_id": "7"}
            summary = await tg.dispatch_queued(review_ref=review_ref)
        return summary, client, mint_mock, send_mock

    async def test_claim_once_send_once_with_buttons(self):
        async def ok_flow(ref, flow, sender, **kwargs):
            return {"token": "tok-correct", "request_key": "tgflow-1",
                "result": {"status": "minted"}}

        summary, client, mint_mock, send_mock = \
            await self._run_dispatch([queued_row()], flow_mint=ok_flow)
        self.assertEqual(summary, {"scanned": 1, "sent": 1, "failed": 0,
            "claim_conflicts": 0, "deferred": 0, "retried": 0})
        self.assertEqual(mint_mock.call_count, 2)
        actions = sorted(c.args[1] for c in mint_mock.call_args_list)
        self.assertEqual(actions, ["approve", "reject"])
        keyboard = send_mock.call_args.kwargs["reply_markup"]
        tokens = [b["callback_data"]
            for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(len(tokens), 3)
        self.assertEqual(len(set(tokens)), 3)
        labels = [b["text"]
            for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(labels, ["Approve", "Correct", "Reject"])
        for token in tokens:
            self.assertNotIn(REF, token)
            self.assertNotIn("tgcb", token)
            self.assertNotIn("tgflow", token)
        sent_patch = [c for c in client.patch.call_args_list
            if c.kwargs["json"].get("status") == "sent"]
        self.assertEqual(len(sent_patch), 1)
        self.assertIn("provider_message_id", sent_patch[0].kwargs["json"])

    async def test_concurrent_claim_conflict_sends_nothing(self):
        summary, _, _, send_mock = await self._run_dispatch(
            [queued_row(status="sending")])
        self.assertEqual(summary["claim_conflicts"], 1)
        send_mock.assert_not_called()

    async def test_forged_outbound_key_fails_closed_without_send(self):
        row = queued_row()
        row["idempotency_key"] = "tg_review_req:tampered"
        summary, _, mint_mock, send_mock = \
            await self._run_dispatch([row])
        self.assertEqual(summary["failed"], 1)
        mint_mock.assert_not_called()
        send_mock.assert_not_called()

    async def test_send_outage_requeues_with_backoff_for_retry(self):
        async def down(chat_id, text, reply_markup=None):
            raise outbound_telegram_module.OutboundUnavailable("no token")

        summary, client, _, send_mock = await self._run_dispatch(
            [queued_row()], send=down)
        self.assertEqual(summary["retried"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["sent"], 0)
        send_mock.assert_called_once()
        requeued = [c for c in client.patch.call_args_list
            if c.kwargs["json"].get("status") == "queued"]
        self.assertEqual(len(requeued), 1)
        body = requeued[0].kwargs["json"]
        self.assertEqual(body["attempt_count"], 1)
        self.assertIn("next_attempt_at", body)
        self.assertNotIn(BOT_TOKEN, body.get("failure_reason", ""))

    async def test_send_outage_fails_terminally_after_max_attempts(self):
        async def down(chat_id, text, reply_markup=None):
            raise outbound_telegram_module.OutboundUnavailable("no token")

        row = queued_row()
        row["attempt_count"] = 4
        summary, client, _, send_mock = await self._run_dispatch(
            [row], send=down)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["retried"], 0)
        failed = [c for c in client.patch.call_args_list
            if c.kwargs["json"].get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].kwargs["json"]["attempt_count"], 5)

    async def test_future_retry_row_is_deferred_not_sent(self):
        row = queued_row()
        row["next_attempt_at"] = "2999-01-01T00:00:00+00:00"
        summary, _, _, send_mock = await self._run_dispatch([row])
        self.assertEqual(summary, {"scanned": 1, "sent": 0, "failed": 0,
            "claim_conflicts": 0, "deferred": 1, "retried": 0})
        send_mock.assert_not_called()

    async def test_reporter_ack_dispatch_sends_plain_text(self):
        row = queued_row(message_type="review_confirmed",
            key="review_ack:%s:tgintake:inbox-9" % TENANT_UUID)
        row["message_text"] = "Approved \u2705\n1 bag sold."
        summary, _, _, send_mock = await self._run_dispatch([row])
        self.assertEqual(summary["sent"], 1)
        self.assertIsNone(send_mock.call_args.kwargs["reply_markup"])

    async def test_mint_refusal_for_reject_still_sends_approve_only(self):
        async def selective(ref, action, sender):
            if action == "reject":
                raise tg.TelegramCallbackError("UNAUTHORIZED")
            return {"token": "tok-approve", "request_key": "tgcb-a"}

        summary, _, mint_mock, send_mock = await self._run_dispatch(
            [queued_row()], mint=selective)
        self.assertEqual(summary["sent"], 1)
        keyboard = send_mock.call_args.kwargs["reply_markup"]
        tokens = [b["callback_data"]
            for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(tokens, ["tok-approve"])

    async def test_unauthorized_reviewer_never_mints_or_sends(self):
        async def denied(ref, action, sender):
            raise tg.TelegramCallbackError("UNAUTHORIZED")

        summary, _, mint_mock, send_mock = await self._run_dispatch(
            [queued_row()], mint=denied)
        self.assertEqual(summary["failed"], 1)
        send_mock.assert_not_called()


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_conflict_never_dispatches(self):
        async def conflict(submission_id, request_key):
            raise tg.TelegramAdapterError("CONFLICT: reused key")

        with patch.object(tg, "queue_telegram_reviews",
                side_effect=conflict), \
             patch.object(tg, "dispatch_queued",
                new_callable=AsyncMock) as dispatch:
            summary = await tg.sync_submission_reviews(
                SUBMISSION_UUID, "tgsync-1")
            dispatch.assert_not_called()
        self.assertIn("refused", summary["error"])
        self.assertIsNone(summary["dispatch"])

    async def test_terminal_queue_outcome_skips_dispatch(self):
        async def no_reviewer(submission_id, request_key):
            return {"status": "no_eligible_reviewer",
                "review_ref": REF, "notified": [], "skipped": [],
                "is_retry": False}

        with patch.object(tg, "queue_telegram_reviews",
                side_effect=no_reviewer), \
             patch.object(tg, "dispatch_queued",
                new_callable=AsyncMock) as dispatch:
            summary = await tg.sync_submission_reviews(
                SUBMISSION_UUID, "tgsync-1")
            dispatch.assert_not_called()
        self.assertEqual(summary["queue_status"], "no_eligible_reviewer")

    async def test_queued_triggers_dispatch_for_reference(self):
        async def queued(submission_id, request_key):
            return {"status": "queued", "review_ref": REF,
                "notified": [], "skipped": [], "is_retry": False}

        with patch.object(tg, "queue_telegram_reviews",
                side_effect=queued), \
             patch.object(tg, "dispatch_queued",
                new_callable=AsyncMock,
                return_value={"scanned": 1}) as dispatch:
            summary = await tg.sync_submission_reviews(
                SUBMISSION_UUID, "tgsync-1")
            self.assertEqual(dispatch.call_count, 2)
            dispatch.assert_any_call(review_ref=REF)
            dispatch.assert_any_call(limit=20)
        self.assertEqual(summary["queue_status"], "queued")


class InternalEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, path, body, key=None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                base_url="http://test") as client:
            headers = {}
            if key is not None:
                headers["x-yamsi-key"] = key
            return await client.post(path, json=body, headers=headers)

    async def test_link_endpoint_requires_owner_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "owner-key"}):
            response = await self.post("/internal/telegram-link",
                {"tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                    "request_key": "k"})
        self.assertEqual(response.status_code, 401)

    async def test_sync_endpoint_requires_owner_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "owner-key"}):
            response = await self.post("/internal/telegram-review-sync",
                {"submission_id": SUBMISSION_UUID})
        self.assertEqual(response.status_code, 401)

    async def test_link_endpoint_issues_single_use_url(self):
        async def fake_issue(tenant_id, employee_id, request_key, ttl=None):
            return {"link_url": "https://t.me/x?start=SECRETTOKEN",
                "is_retry": False}

        with patch.dict(os.environ, {"YAMSI_API_KEY": "owner-key"}), \
             patch.object(tg, "issue_employee_link",
                side_effect=fake_issue):
            response = await self.post("/internal/telegram-link",
                {"tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                    "request_key": "k"}, key="owner-key")
        self.assertEqual(response.status_code, 200)
        self.assertIn("link_url", response.json())

    async def test_sync_endpoint_returns_summary(self):
        async def fake_sync(submission_id, request_key):
            return {"queue_status": "queued", "dispatch": {"sent": 1},
                "error": None}

        with patch.dict(os.environ, {"YAMSI_API_KEY": "owner-key"}), \
             patch.object(tg, "sync_submission_reviews",
                side_effect=fake_sync):
            response = await self.post("/internal/telegram-review-sync",
                {"submission_id": SUBMISSION_UUID}, key="owner-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queue_status"], "queued")


class OutboundTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_fails_without_network(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}), \
             patch.object(outbound_telegram_module.httpx, "AsyncClient") \
                as factory:
            with self.assertRaises(
                    outbound_telegram_module.OutboundUnavailable):
                await outbound_telegram_module.send_message(CHAT_ID, "hi")
            factory.assert_not_called()

    async def test_token_never_appears_in_errors(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": BOT_TOKEN}):
            client = AsyncMock()
            client.post = AsyncMock(
                return_value=httpx.Response(403, json={"ok": False}))
            with patch.object(outbound_telegram_module.httpx,
                    "AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                with self.assertRaises(
                        outbound_telegram_module.OutboundUnavailable) as ctx:
                    await outbound_telegram_module.send_message(CHAT_ID, "hi")
            self.assertNotIn(BOT_TOKEN, str(ctx.exception))
            sent_url = client.post.call_args.args[0]
            self.assertIn("/sendMessage", sent_url)

    async def test_callback_answer_never_raises(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            await outbound_telegram_module.answer_callback_query("cq-1")
            await outbound_telegram_module.clear_inline_keyboard(
                CHAT_ID, 7)


class FailureNoteTests(unittest.TestCase):
    def test_notes_carry_no_contents_or_secrets(self):
        secretish = human_confirmation.WorkflowDatabaseError(
            "db down " + BOT_TOKEN + " chat " + CHAT_ID)
        note = tg._failure_note(secretish)
        self.assertNotIn(BOT_TOKEN, note)
        self.assertNotIn(CHAT_ID, note)
        self.assertIn("WorkflowDatabaseError", note)
        refused = tg._failure_note(
            human_confirmation.ReviewUnauthorizedError("UNAUTHORIZED"))
        self.assertIn("refused", refused)


class ChannelBoundaryTests(unittest.TestCase):
    def _sources(self):
        import pathlib
        root = pathlib.Path(__file__).parent
        return {name: (root / name).read_text(encoding="utf-8")
            for name in ("telegram_adapter.py", "telegram_webhook.py",
                "outbound_telegram.py", "app.py")}

    def test_no_direct_operational_or_brain_writes(self):
        sources = self._sources()
        for name, text in sources.items():
            for forbidden in ("amose_post_", "operational_posting",
                    "INSERT INTO", "biz_production", "biz_sales",
                    "brain_memory"):
                self.assertNotIn(forbidden, text, "%s must not contain %s"
                    % (name, forbidden))

    def test_no_whatsapp_credential_dependency(self):
        # The Telegram channel files must work when WhatsApp credentials
        # are unavailable. app.py itself wires both routers (pre-existing
        # WhatsApp behavior), so it is excluded from this check.
        sources = self._sources()
        del sources["app.py"]
        for name, text in sources.items():
            for forbidden in ("WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN",
                    "WHATSAPP_VERIFY_TOKEN", "outbound_whatsapp",
                    "whatsapp_webhook"):
                self.assertNotIn(forbidden, text, "%s must not depend on %s"
                    % (name, forbidden))

    def test_no_logging_or_print_of_sensitive_values(self):
        sources = self._sources()
        for name, text in sources.items():
            for forbidden in ("\nimport logging", "print("):
                self.assertNotIn(forbidden, text,
                    "%s must not contain %s" % (name, forbidden))

    def test_only_telegram_rpc_allowlist(self):
        import re
        text = self._sources()["telegram_adapter.py"]
        rpcs = set(re.findall(r'"(amose_[a-z_]+)"', text))
        allowed = {"amose_issue_telegram_link", "amose_consume_telegram_link",
            "amose_queue_telegram_review_requests",
            "amose_mint_telegram_callback",
            "amose_consume_telegram_callback",
            "amose_review_confirm_command", "amose_reject_submission",
            "amose_confirm_submission",
            "amose_mint_telegram_flow_token",
            "amose_consume_telegram_flow_token",
            "amose_stage_telegram_flow", "amose_read_telegram_flows"}
        for rpc in rpcs:
            self.assertIn(rpc, allowed, "unexpected RPC reference: " + rpc)


if __name__ == "__main__":
    unittest.main()
