"""Telegram simple-workflow regression tests.

Covers: automatic outbox dispatch after intake (and its failure
isolation), branch-choice buttons, the guided correct/reject/cancel
button flows with staged values, reporter acknowledgements, short
codes, money-unit conversion (NGN 450 -> 45000 kobo), and the audited
reversal RPC contract. Every database and Telegram API interaction is
mocked: no test contacts a real service or reveals a real secret.
"""
import re
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import human_confirmation
import outbound_telegram as outbound_telegram_module
import review_service as review_service_module
import telegram_adapter as tg
import telegram_intake as intake
import water_intake as water

CHAT_ID = "123456789"
REF = "YR-ABCD234EFG"
TENANT_UUID = "00000000-0000-0000-0000-000000000001"
EMP_UUID = "22222222-2222-2222-2222-222222222222"
SUBMISSION_UUID = "11111111-1111-1111-1111-111111111111"
MIGRATION = ("supabase/migrations/"
    "20260925000000_telegram_simple_workflow.sql")


def message_update(update_id=100, text="hello", chat_id=CHAT_ID,
        sender_id=None):
    sender = int(sender_id if sender_id is not None else chat_id)
    return {"update_id": update_id,
        "message": {"message_id": 1,
            "from": {"id": sender, "first_name": "Some Name"},
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


def read_migration():
    with open(MIGRATION, encoding="utf-8") as handle:
        return handle.read()


class AutoDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def _run_intake(self, dispatch=None):
        client = mock_batch_client()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[{
                    "tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=[{
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": SUBMISSION_UUID}])
            return httpx.Response(200, json=[])

        client.get = AsyncMock(side_effect=fake_get)
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text="SALE 1 bag for 450 naira cash"))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "queue_telegram_reviews",
                new_callable=AsyncMock) as queue_mock, \
             patch.object(tg, "dispatch_queued",
                new_callable=AsyncMock) as dispatch_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            queue_mock.return_value = {"status": "queued",
                "review_ref": REF, "notified": [EMP_UUID], "skipped": [],
                "is_retry": False}
            dispatch_mock.side_effect = dispatch
            if dispatch is None:
                dispatch_mock.return_value = {"scanned": 1, "sent": 1,
                    "failed": 0, "claim_conflicts": 0, "deferred": 0,
                    "retried": 0}
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        return outcome, summary, dispatch_mock

    async def test_queued_draft_dispatches_automatically(self):
        outcome, summary, dispatch_mock = await self._run_intake()
        self.assertEqual(outcome["outcome"], "intake")
        dispatch_mock.assert_called_once_with(review_ref=REF)
        self.assertEqual(summary["notifications_sent"], 1)

    async def test_dispatch_failure_never_loses_the_draft(self):
        async def boom(**kwargs):
            raise RuntimeError("sender down")

        outcome, summary, dispatch_mock = await self._run_intake(
            dispatch=boom)
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(summary["intakes_submitted"], 1)
        dispatch_mock.assert_called_once_with(review_ref=REF)


class BranchButtonTests(unittest.IsolatedAsyncioTestCase):
    async def _run_text(self, text, assignments, stage=None):
        client = mock_batch_client()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[{
                    "tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=list(assignments))
            return httpx.Response(200, json=[])

        async def no_stage(*args, **kwargs):
            raise tg.TelegramLinkError("NOT_FOUND: no pending guided step")

        client.get = AsyncMock(side_effect=fake_get)
        summary = tg._new_summary()
        update = tg.parse_update(message_update(text=text))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "mint_flow_token",
                new_callable=AsyncMock) as mint_mock, \
             patch.object(tg, "stage_flow",
                new_callable=AsyncMock) as stage_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client

            async def ok_mint(ref, flow, sender, **kwargs):
                return {"token": "tok-%s-%s" % (
                    flow, kwargs.get("branch_id") or "x"),
                    "request_key": "tgflow-1",
                    "result": {"status": "minted"}}

            mint_mock.side_effect = ok_mint
            stage_mock.side_effect = stage or no_stage
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        return outcome, summary, mint_mock, send_mock

    def _assignments(self):
        return [{"business_id": "amose_table_water", "branch_id": "asaba"},
            {"business_id": "amose_table_water", "branch_id": "warri"}]

    async def test_unscoped_report_offers_branch_buttons(self):
        outcome, summary, mint_mock, send_mock = await self._run_text(
            "SALE 1 bag for 450 naira cash", self._assignments())
        self.assertEqual(outcome["outcome"], "help")
        self.assertEqual(mint_mock.call_count, 2)
        for call in mint_mock.call_args_list:
            self.assertIsNone(call.args[0])
            self.assertEqual(call.args[1], "report_branch")
        keyboard = send_mock.call_args.kwargs["reply_markup"]
        labels = [b["text"]
            for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(sorted(labels), ["Asaba", "Warri"])
        tokens = [b["callback_data"]
            for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(len(set(tokens)), 2)
        for token in tokens:
            self.assertNotIn(REF, token)
            self.assertNotIn("tgflow", token)
            self.assertNotIn("business", token)

    async def test_stored_branch_choice_routes_next_report(self):
        posted = []
        client = mock_batch_client()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[{
                    "tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=self._assignments())
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": SUBMISSION_UUID}])
            return httpx.Response(200, json=[])

        async def fake_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/biz_submissions":
                posted.extend(json or [])
            return httpx.Response(201, json={})

        async def take_branch(*args, **kwargs):
            if kwargs.get("take"):
                return {"status": "taken", "flow": "report_branch",
                    "field": "warri", "business_id": "amose_table_water",
                    "branch_id": "warri"}
            raise tg.TelegramLinkError("NOT_FOUND: no pending guided step")

        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(side_effect=fake_post)
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text="SALE 1 bag for 450 naira cash"))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "queue_telegram_reviews",
                new_callable=AsyncMock) as queue_mock, \
             patch.object(tg, "dispatch_queued",
                new_callable=AsyncMock) as dispatch_mock, \
             patch.object(tg, "stage_flow",
                new_callable=AsyncMock) as stage_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            queue_mock.return_value = {"status": "queued",
                "review_ref": REF, "notified": [], "skipped": [],
                "is_retry": False}
            dispatch_mock.return_value = {"scanned": 0, "sent": 0,
                "failed": 0, "claim_conflicts": 0, "deferred": 0,
                "retried": 0}
            stage_mock.side_effect = take_branch
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(posted[0]["branch_id"], "warri")


class GuidedCorrectTests(unittest.IsolatedAsyncioTestCase):
    async def _run_press(self, flowed, confirm=None, mint=None,
            stage=None):
        client = mock_batch_client()
        summary = tg._new_summary()
        update = tg.parse_update(callback_update())

        async def no_stage(*args, **kwargs):
            raise tg.TelegramLinkError("NOT_FOUND: no pending guided step")

        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "consume_flow_token",
                new_callable=AsyncMock) as flow_mock, \
             patch.object(tg, "mint_flow_token",
                new_callable=AsyncMock) as mint_mock, \
             patch.object(tg, "stage_flow",
                new_callable=AsyncMock) as stage_mock, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock) as confirm_mock, \
             patch.object(outbound_telegram_module, "answer_callback_query",
                new_callable=AsyncMock), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock, \
             patch.object(outbound_telegram_module, "clear_inline_keyboard",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            flow_mock.return_value = dict(flowed)
            if mint is not None:
                mint_mock.side_effect = mint
            else:
                async def ok_mint(*args, **kwargs):
                    return {"token": "tok-flow",
                        "request_key": "tgflow-1",
                        "result": {"status": "minted"}}
                mint_mock.side_effect = ok_mint
            stage_mock.side_effect = stage or no_stage
            if confirm is not None:
                confirm_mock.side_effect = confirm
            outcome = await tg._process_callback(
                client, "inbox-2", update["callback_query"], summary)
        return outcome, summary, send_mock, confirm_mock, mint_mock

    def _menu_press(self):
        return {"status": "ready", "flow": "correct_menu", "field": None,
            "review_ref": REF, "submission_kind": "sale",
            "request_key": "tgflow-1", "is_retry": False}

    async def test_correct_menu_lists_plain_fields_and_cancel(self):
        outcome, _, send_mock, _, _ = await self._run_press(
            self._menu_press())
        self.assertEqual(outcome["outcome"], "flow_correct_menu")
        reply = send_mock.call_args.args[1]
        self.assertIn("What is wrong?", reply)
        keyboard = send_mock.call_args.kwargs["reply_markup"]
        labels = [b["text"]
            for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(
            labels, ["Quantity", "Price", "Unit", "Cancel"])

    async def test_field_press_asks_for_plain_value(self):
        async def staged(sender, **kwargs):
            return {"status": "staged"}

        outcome, _, send_mock, _, _ = await self._run_press(
            {"status": "ready", "flow": "correct_field",
                "field": "quantity", "review_ref": REF,
                "submission_kind": "sale", "request_key": "tgflow-1",
                "is_retry": False},
            stage=staged)
        self.assertEqual(outcome["outcome"], "flow_correct_field")
        reply = send_mock.call_args.args[1]
        self.assertIn("number", reply)
        self.assertNotIn("quantity", reply.split("number")[0])

    async def test_save_runs_confirm_with_corrections_once(self):
        seen = {}

        async def staged(sender, **kwargs):
            if kwargs.get("clear"):
                return {"status": "cleared"}
            return {"status": "staged", "flow": "correct",
                "field": "quantity", "value_text": "1"}

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            seen.update(key=key, reason=correction_reason,
                corrections=corrections)
            return {"status": "confirmed"}

        outcome, summary, send_mock, confirm_mock, _ = \
            await self._run_press(
                {"status": "ready", "flow": "correct_save",
                    "review_ref": REF, "submission_kind": "sale",
                    "request_key": "tgflow-1", "is_retry": False},
                confirm=confirmed, stage=staged)
        self.assertEqual(outcome["outcome"], "confirm")
        confirm_mock.assert_called_once()
        self.assertEqual(seen["corrections"], {"quantity": "1"})
        self.assertTrue(seen["key"].startswith("tgflowsave:"))
        self.assertTrue(seen["reason"])
        reply = send_mock.call_args.args[1]
        self.assertIn("approved", reply.lower())

    async def test_save_double_press_reuses_idempotent_key(self):
        calls = []

        async def staged(sender, **kwargs):
            return {"status": "staged", "flow": "correct",
                "field": "quantity", "value_text": "1"}

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            calls.append(key)
            return {"status": "confirmed", "is_retry": True}

        for _ in range(2):
            outcome, _, _, _, _ = await self._run_press(
                {"status": "already_used", "flow": "correct_save",
                    "review_ref": REF, "submission_kind": "sale",
                    "request_key": "tgflow-1", "is_retry": True},
                confirm=confirmed, stage=staged)
            self.assertEqual(outcome["outcome"], "confirm")
        self.assertEqual(calls, ["tgflowsave:inbox-2", "tgflowsave:inbox-2"])

    async def test_forged_flow_press_refused(self):
        with patch.object(tg, "consume_flow_token",
                new_callable=AsyncMock) as flow_mock, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock) as confirm_mock, \
             patch.object(outbound_telegram_module, "answer_callback_query",
                new_callable=AsyncMock), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            async def forged(token, sender):
                raise tg.TelegramCallbackError(
                    "UNAUTHORIZED: reviewer could not be authorized")

            flow_mock.side_effect = forged
            client = mock_batch_client()
            summary = tg._new_summary()
            update = tg.parse_update(callback_update())
            outcome = await tg._process_callback(
                client, "inbox-2", update["callback_query"], summary)
        self.assertEqual(outcome["outcome"], "refused")
        confirm_mock.assert_not_called()


class GuidedRejectTests(unittest.IsolatedAsyncioTestCase):
    async def _run_text(self, text, staged_flow="reject", reject=None):
        client = mock_batch_client()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[{
                    "tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
                    "business_id": "amose_table_water",
                    "branch_id": "asaba"}])
            return httpx.Response(200, json=[])

        client.get = AsyncMock(side_effect=fake_get)
        summary = tg._new_summary()
        update = tg.parse_update(message_update(text=text))

        async def flows(sender):
            return [{"review_ref": REF, "flow": staged_flow,
                "field": None, "submission_kind": "sale"}]

        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "read_flows",
                new_callable=AsyncMock) as flows_mock, \
             patch.object(tg, "stage_flow",
                new_callable=AsyncMock) as stage_mock, \
             patch.object(review_service_module, "reject_from_chat",
                new_callable=AsyncMock) as reject_mock, \
             patch.object(tg, "dispatch_queued",
                new_callable=AsyncMock) as dispatch_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            flows_mock.side_effect = flows
            stage_mock.return_value = {"status": "staged"}
            dispatch_mock.return_value = {"scanned": 0, "sent": 0,
                "failed": 0, "claim_conflicts": 0, "deferred": 0,
                "retried": 0}
            if reject is not None:
                reject_mock.side_effect = reject
            outcome = await tg.process_stored_update(
                update, "inbox-9", summary)
        return outcome, summary, send_mock, reject_mock, dispatch_mock

    async def test_reason_text_runs_guided_rejection(self):
        async def rejected(client, ref, provider, sender, key, reason=None):
            return {"status": "rejected"}

        outcome, summary, send_mock, reject_mock, dispatch_mock = \
            await self._run_text("wrong price, sorry", reject=rejected)
        self.assertEqual(outcome["outcome"], "reject")
        reject_mock.assert_called_once()
        args = reject_mock.call_args.args
        self.assertEqual(args[1:4], (REF, "telegram", CHAT_ID))
        self.assertEqual(reject_mock.call_args.kwargs["reason"],
            "wrong price, sorry")
        reply = send_mock.call_args.args[1]
        self.assertEqual(reply, "Rejected.")
        dispatch_mock.assert_called_once()
        self.assertEqual(summary["reviews_rejected"], 1)

    async def test_blank_message_gets_help_not_rejection(self):
        outcome, _, send_mock, reject_mock, _ = await self._run_text(
            "   ")
        self.assertEqual(outcome["outcome"], "help")
        reject_mock.assert_not_called()

    async def test_blank_reason_reasks_without_rejecting(self):
        client = mock_batch_client()
        summary = tg._new_summary()
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "reject_from_chat",
                new_callable=AsyncMock) as reject_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg._answer_reject_reason(
                client, "inbox-9", CHAT_ID, CHAT_ID, REF, "   ",
                summary)
        self.assertEqual(outcome["outcome"], "flow_reason_invalid")
        reject_mock.assert_not_called()
        reply = send_mock.call_args.args[1]
        self.assertIn("short reason", reply)


class SaleMoneyUnitTests(unittest.TestCase):
    def test_sale_unit_price_stays_naira_in_draft(self):
        parsed = water._parse_sale("sold 1 bag for 450 naira cash")
        self.assertEqual(parsed["kind"], "sale")
        self.assertEqual(parsed["fields"]["unit_price"], 450.0)

    def test_sale_preview_shows_whole_naira(self):
        extraction = {"kind": "sale", "fields": {"quantity": 1.0,
            "unit": "bag", "unit_price": 450.0,
            "payment_method": "cash"}, "missing_fields": [],
            "errors": []}
        text = intake.format_intake_preview(extraction)
        self.assertIn("\u20a6450", text)
        self.assertNotIn("45000", text)


class MigrationContractTests(unittest.TestCase):
    def test_sale_branch_converts_naira_to_kobo_once(self):
        sql = read_migration()
        self.assertIn(
            "'unit_price_kobo', (v_price * 100)::bigint,", sql)
        self.assertNotIn("unit_price_kobo', (v_price * 100)::bigint * 100",
            sql)

    def test_reversal_rpc_is_audited_and_idempotent(self):
        sql = read_migration()
        self.assertIn(
            "create or replace function "
            "public.amose_reverse_confirmed_submission(", sql)
        self.assertIn("biz_review_reversals", sql)
        self.assertIn("'is_retry', true", sql)
        self.assertIn(
            "UNAUTHORIZED: the reporter cannot reverse", sql)
        self.assertIn(
            "request key was already used for a different reversal",
            sql)
        self.assertIn(
            "revoke execute on function "
            "public.amose_reverse_confirmed_submission(", sql)

    def test_case_guard_allows_confirmed_to_reversed(self):
        sql = read_migration()
        self.assertIn(
            "create or replace function "
            "public._amose_guard_review_cases_immutable()", sql)
        self.assertIn(
            "OLD.status = 'confirmed'\n          and NEW.status = 'reversed'",
            sql)
        self.assertIn(
            "a decided review case cannot be reopened", sql)

    def test_short_code_helper_is_wired(self):
        sql = read_migration()
        self.assertIn(
            "create or replace function public._amose_short_code(", sql)
        self.assertGreater(sql.count("_amose_short_code("), 2)

    def test_flow_rpcs_present_and_revoked(self):
        sql = read_migration()
        for name in ("amose_mint_telegram_flow_token",
                "amose_consume_telegram_flow_token",
                "amose_stage_telegram_flow",
                "amose_read_telegram_flows"):
            self.assertIn(
                "create or replace function public.%s(" % name, sql)
            self.assertIn(
                "revoke execute on function public.%s(" % name, sql)

    def test_python_flow_allowlists_match_migration(self):
        sql = read_migration()
        pairs = ((tg.FLOW_MINT_RPC, tg.FLOW_MINT_ALLOWLIST),
            (tg.FLOW_CONSUME_RPC, tg.FLOW_CONSUME_ALLOWLIST),
            (tg.FLOW_STAGE_RPC, tg.FLOW_STAGE_ALLOWLIST),
            (tg.FLOW_READ_RPC, tg.FLOW_READ_ALLOWLIST))
        for rpc, allowlist in pairs:
            with self.subTest(rpc=rpc):
                self.assertEqual(allowlist, frozenset({rpc}))
                self.assertIn(
                    "create or replace function public.%s(" % rpc,
                    sql)
