"""End-to-end confirmation for all seven Telegram intake kinds.

Proves the full loop with mocked transports only (no live database, no
real network): intake draft -> review request -> REVIEW CONFIRM /
CORRECTION / REVIEW REJECT over Telegram -> shared review boundary;
plus replay safety, cross-tenant refusal, duplicate-message safety, the
Python mapping/validator sync for the new posting types, and static
checks that the Supabase migration wires every kind through secured,
service-role-only RPCs.

Kind-specific confirmed payloads mirror the migration's posting RPC
result shapes (status/posting_type/IDs/audit/verified echo), so the
strict Python response validation runs for real on every kind.
"""
import pathlib
import re
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import human_confirmation
import operational_posting
import outbound_telegram as outbound_telegram_module
import review_service as review_service_module
import telegram_adapter as tg

CHAT_ID = "123456789"
REF = "YR-ABCD234EFG"
SUBMISSION_UUID = "11111111-1111-1111-1111-111111111111"
TENANT_UUID = "00000000-0000-0000-0000-000000000001"
EMP_UUID = "22222222-2222-2222-2222-222222222222"
PRODUCT_UUID = "33333333-3333-3333-3333-333333333333"
CUSTOMER_UUID = "44444444-4444-4444-4444-444444444444"
BUSINESS_ID = "amose_table_water"
BRANCH_ID = "asaba"

INTAKE_TEXTS = {
    "sale": "SALE 50 bags at 500 cash",
    "production": "PRODUCTION 225 bags used 7kg nylon",
    "expense": "EXPENSE fuel 15000",
    "bank_deposit": "DEPOSIT 80000 to Access Bank ref ABC123",
    "stock": "STOCK 120 normal bags and 75 cold bags",
    "customer_payment": "CUSTOMER PAYMENT Emeka 25000 transfer",
    "customer_debt": "CUSTOMER DEBT Ada 12000",
}

CONFIRMED_RESULTS = {
    "sale": {"status": "confirmed", "review_action": "confirmed",
        "submission_kind": "sale", "posting_type": "sale",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1", "sale_id": "s1",
        "brain_memory_id": "m1", "verified_snapshot": {"kind": "sale"}},
    "production": {"status": "confirmed", "review_action": "confirmed",
        "submission_kind": "production", "posting_type": "production",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1", "production_run_id": "s1",
        "brain_memory_id": "m1",
        "verified_snapshot": {"kind": "production"}},
    "expense": {"status": "confirmed", "review_action": "confirmed",
        "submission_kind": "expense", "posting_type": "expense",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1", "expense_id": "s1",
        "brain_memory_id": "m1",
        "verified_snapshot": {"kind": "expense"}},
    "bank_deposit": {"status": "confirmed", "review_action": "confirmed",
        "submission_kind": "bank_deposit", "posting_type": "bank_deposit",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1",
        "cash_custody_entry_id": "s1", "brain_memory_id": "m1",
        "verified_snapshot": {"kind": "bank_deposit"}},
    "stock": {"status": "confirmed", "review_action": "confirmed",
        "submission_kind": "stock", "posting_type": "stock",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1", "stock_count_id": "s1",
        "brain_memory_id": "m1",
        "verified_snapshot": {"kind": "stock"}},
    "customer_payment": {"status": "confirmed",
        "review_action": "confirmed", "submission_kind": "customer_payment",
        "posting_type": "customer_payment",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1", "customer_payment_id": "s1",
        "cash_custody_entry_id": "c1", "brain_memory_id": "m1",
        "verified_snapshot": {"kind": "customer_payment"}},
    "customer_debt": {"status": "confirmed", "review_action": "confirmed",
        "submission_kind": "customer_debt", "posting_type": "customer_debt",
        "submission_id": SUBMISSION_UUID, "review_ref": REF,
        "request_key": "k1", "audit_id": "a1", "customer_debt_id": "s1",
        "brain_memory_id": "m1",
        "verified_snapshot": {"kind": "customer_debt"}},
}


def message_update(update_id=100, text="hello", chat_id=CHAT_ID,
        sender_id=None):
    sender = int(sender_id if sender_id is not None else chat_id)
    return {"update_id": update_id,
        "message": {"message_id": 1,
            "from": {"id": sender, "first_name": "Staff Member",
                "username": "staff_member"},
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text}}


def intake_client(identity_rows=None, assignments=None):
    identity_rows = identity_rows if identity_rows is not None else [
        {"tenant_id": TENANT_UUID, "employee_id": EMP_UUID,
            "business_id": BUSINESS_ID, "branch_id": BRANCH_ID}]
    assignments = assignments if assignments is not None else [
        {"business_id": BUSINESS_ID, "branch_id": BRANCH_ID}]
    client = AsyncMock()

    async def fake_get(path, params=None):
        if path == "/rest/v1/biz_sender_identities":
            return httpx.Response(200, json=list(identity_rows))
        if path == "/rest/v1/biz_assignments":
            return httpx.Response(200, json=list(assignments))
        if path == "/rest/v1/biz_submissions":
            return httpx.Response(200, json=[{"id": SUBMISSION_UUID}])
        return httpx.Response(200, json=[])

    async def fake_post(path, json=None, params=None, headers=None):
        if path == "/rest/v1/rpc/amose_queue_review_requests":
            return httpx.Response(200, json={"status": "queued",
                "review_ref": REF,
                "submission_id": json["p_submission_id"],
                "submission_kind": "sale",
                "request_key": json["p_request_key"],
                "notified": 1, "skipped": [], "is_retry": False})
        return httpx.Response(201, json={})

    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(side_effect=fake_post)
    client.patch = AsyncMock(return_value=httpx.Response(204))
    return client


class ConfirmLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _full_loop(self, kind):
        """Intake one report, then confirm it over Telegram; returns the
        recorded calls and replies for assertions."""
        posted = []
        client = intake_client()

        async def capture_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/biz_submissions":
                posted.extend(json or [])
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                return httpx.Response(200, json={"status": "queued",
                    "review_ref": REF,
                    "submission_id": json["p_submission_id"],
                    "submission_kind": kind,
                    "request_key": json["p_request_key"],
                    "notified": 1, "skipped": [], "is_retry": False})
            return httpx.Response(201, json={})

        client.post = AsyncMock(side_effect=capture_post)

        async def queued(submission_id, request_key):
            return {"status": "queued", "review_ref": REF,
                "submission_id": submission_id, "request_key": request_key,
                "notified": [EMP_UUID], "skipped": [], "is_retry": False}

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            self.assertEqual(corrections, {})
            result = dict(CONFIRMED_RESULTS[kind])
            result["request_key"] = key
            return result

        summary = tg._new_summary()
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "queue_telegram_reviews",
                new_callable=AsyncMock, side_effect=queued), \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock,
                side_effect=confirmed) as confirm_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            intake_update = tg.parse_update(message_update(
                update_id=100, text=INTAKE_TEXTS[kind]))
            intake_outcome = await tg.process_stored_update(
                intake_update, "inbox-1", summary)
            confirm_update = tg.parse_update(message_update(
                update_id=101,
                text="REVIEW CONFIRM %s KEY k1" % REF))
            confirm_outcome = await tg.process_stored_update(
                confirm_update, "inbox-2", summary)
        replies = [c.args[1] for c in send_mock.call_args_list]
        return intake_outcome, confirm_outcome, summary, posted, \
            confirm_mock, replies

    async def test_confirm_loop_for_every_kind(self):
        for kind in INTAKE_TEXTS:
            with self.subTest(kind=kind):
                intake_outcome, confirm_outcome, summary, posted, \
                    confirm_mock, replies = await self._full_loop(kind)
                self.assertEqual(intake_outcome["outcome"], "intake")
                self.assertEqual(intake_outcome["kind"], kind)
                self.assertTrue(posted)
                self.assertEqual(posted[0]["kind"], kind)
                self.assertEqual(confirm_outcome["outcome"], "confirm")
                confirm_mock.assert_called_once()
                args = confirm_mock.call_args.args
                self.assertEqual(args[1:5], (REF, "telegram", CHAT_ID, "k1"))
                self.assertIn(REF, replies[-1])
                self.assertIn("Recorded confirm", replies[-1])

    async def test_correction_carries_reason(self):
        async def corrected(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            self.assertEqual(correction_reason, "count was 125 not 120")
            self.assertEqual(corrections, {})
            return dict(CONFIRMED_RESULTS["stock"],
                request_key=key, review_action="corrected")

        client = intake_client()
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text="REVIEW CONFIRM %s KEY k1 CORRECTION count was 125 not 120"
            % REF))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock,
                side_effect=corrected) as confirm_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-3", summary)
        self.assertEqual(outcome["outcome"], "confirm")
        confirm_mock.assert_called_once()

    async def test_correction_values_reach_confirm_rpc(self):
        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            self.assertEqual(corrections,
                {"amount": "25000", "method": "cash"})
            self.assertEqual(correction_reason, "recount")
            return dict(CONFIRMED_RESULTS["customer_payment"],
                request_key=key, review_action="corrected")

        client = intake_client()
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text="REVIEW CONFIRM %s KEY k1 CORRECTION amount=25000 "
            "method=cash -- recount" % REF))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock,
                side_effect=confirmed) as confirm_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-3b", summary)
        self.assertEqual(outcome["outcome"], "confirm")
        confirm_mock.assert_called_once()

    async def test_rejection_cancels_with_reason(self):
        async def rejected(client, ref, provider, sender, reason, key):
            self.assertEqual(reason, "duplicate of morning report")
            return {"status": "rejected", "review_action": "rejected",
                "submission_id": SUBMISSION_UUID, "review_ref": ref,
                "request_key": key, "reason": reason, "audit_id": "a9"}

        client = intake_client()
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text="REVIEW REJECT %s KEY k1 REASON duplicate of morning report"
            % REF))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "reject_from_chat",
                new_callable=AsyncMock,
                side_effect=rejected) as reject_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-4", summary)
        self.assertEqual(outcome["outcome"], "reject")
        reject_mock.assert_called_once()
        self.assertIn(REF, send_mock.call_args.args[1])

    async def test_approve_button_confirms_non_sale_kind(self):
        async def ready(token, sender):
            return {"status": "ready", "review_ref": REF,
                "action": "approve", "request_key": "tgcb-" + token,
                "is_retry": False}

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            return dict(CONFIRMED_RESULTS["customer_debt"],
                request_key=key)

        client = intake_client()
        summary = tg._new_summary()
        update = tg.parse_update({"update_id": 102,
            "callback_query": {"id": "cq-9",
                "from": {"id": int(CHAT_ID)},
                "message": {"message_id": 3, "chat": {"id": int(CHAT_ID)}},
                "data": "e" * 32}})
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "consume_button_token",
                new_callable=AsyncMock, side_effect=ready), \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock,
                side_effect=confirmed) as confirm_mock, \
             patch.object(outbound_telegram_module, "answer_callback_query",
                new_callable=AsyncMock), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock, \
             patch.object(outbound_telegram_module, "clear_inline_keyboard",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-5", summary)
        self.assertEqual(outcome["outcome"], "confirm")
        confirm_mock.assert_called_once()
        self.assertIn(REF, send_mock.call_args.args[1])
        self.assertEqual(summary["reviews_confirmed"], 1)

    async def test_cross_tenant_confirm_refused(self):
        async def denied(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            raise human_confirmation.ReviewUnauthorizedError(
                "UNAUTHORIZED: reviewer could not be authorized")

        client = intake_client()
        summary = tg._new_summary()
        update = tg.parse_update(message_update(
            text="REVIEW CONFIRM %s KEY k1" % REF))
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock, side_effect=denied), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-6", summary)
        self.assertEqual(outcome["outcome"], "refused")
        self.assertEqual(outcome["error"], "ReviewUnauthorizedError")
        self.assertEqual(summary["reviews_refused"], 1)
        note = client.patch.call_args.kwargs["json"]
        self.assertEqual(note["status"], "processed")
        reply = send_mock.call_args.args[1]
        self.assertNotIn(SUBMISSION_UUID, reply)

    async def test_confirm_retry_reuses_same_key(self):
        keys = []

        async def confirmed(client, ref, provider, sender, key,
                correction_reason=None, corrections=None):
            keys.append(key)
            return dict(CONFIRMED_RESULTS["expense"], request_key=key)

        client = intake_client()
        summary = tg._new_summary()
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "confirm_from_chat",
                new_callable=AsyncMock, side_effect=confirmed), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            for inbox_id in ("inbox-7", "inbox-8"):
                update = tg.parse_update(message_update(
                    text="REVIEW CONFIRM %s KEY k1" % REF))
                outcome = await tg.process_stored_update(update, inbox_id,
                    summary)
                self.assertEqual(outcome["outcome"], "confirm")
        self.assertEqual(keys, ["k1", "k1"])

    async def test_two_updates_create_two_drafts(self):
        drafts = []
        client = intake_client()

        async def capture_post(path, json=None, params=None, headers=None):
            if path == "/rest/v1/biz_submissions":
                drafts.extend(json or [])
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                return httpx.Response(200, json={"status": "queued",
                    "review_ref": REF,
                    "submission_id": json["p_submission_id"],
                    "submission_kind": "stock",
                    "request_key": json["p_request_key"],
                    "notified": 1, "skipped": [], "is_retry": False})
            return httpx.Response(201, json={})

        client.post = AsyncMock(side_effect=capture_post)

        async def queued(submission_id, request_key):
            return {"status": "queued", "review_ref": REF,
                "submission_id": submission_id, "request_key": request_key,
                "notified": [], "skipped": [], "is_retry": False}

        summary = tg._new_summary()
        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(tg, "queue_telegram_reviews",
                new_callable=AsyncMock, side_effect=queued), \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock):
            factory.return_value.__aenter__.return_value = client
            for update_id, inbox_id in ((200, "inbox-a"), (201, "inbox-b")):
                update = tg.parse_update(message_update(update_id=update_id,
                    text=INTAKE_TEXTS["stock"]))
                outcome = await tg.process_stored_update(
                    update, inbox_id, summary)
                self.assertEqual(outcome["outcome"], "intake")
        self.assertEqual(len(drafts), 2)
        self.assertEqual(
            {d["idempotency_key"] for d in drafts},
            {"inbox-a", "inbox-b"})
        self.assertEqual(summary["intakes_submitted"], 2)


class CancelFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run_cancel(self, text, cancel_result=None,
            cancel_error=None):
        client = intake_client()
        summary = tg._new_summary()
        update = tg.parse_update(message_update(text=text))

        async def cancel(client, ref, provider, sender, key, reason=None):
            if cancel_error is not None:
                raise cancel_error
            return dict(cancel_result, request_key=key)

        with patch.object(tg, "credentials",
                return_value=("https://example.supabase.co", "test-key")), \
             patch.object(tg.httpx, "AsyncClient") as factory, \
             patch.object(review_service_module, "cancel_from_chat",
                new_callable=AsyncMock,
                side_effect=cancel) as cancel_mock, \
             patch.object(outbound_telegram_module, "send_message",
                new_callable=AsyncMock) as send_mock:
            factory.return_value.__aenter__.return_value = client
            outcome = await tg.process_stored_update(
                update, "inbox-cancel", summary)
        return outcome, summary, client, cancel_mock, send_mock

    def _cancelled(self, reason="filed twice"):
        return {"status": "cancelled", "review_action": "cancelled",
            "submission_kind": "stock", "submission_id": SUBMISSION_UUID,
            "review_ref": REF, "request_key": "cancel:inbox-cancel",
            "audit_id": "a7", "reason": reason, "is_retry": False}

    async def test_reporter_cancels_own_draft(self):
        outcome, summary, client, cancel_mock, send_mock = \
            await self._run_cancel("CANCEL %s filed twice" % REF,
                cancel_result=self._cancelled())
        self.assertEqual(outcome["outcome"], "cancelled")
        cancel_mock.assert_called_once()
        args = cancel_mock.call_args.args
        self.assertEqual(args[1:5],
            (REF, "telegram", CHAT_ID, "cancel:inbox-cancel"))
        self.assertEqual(summary["submissions_cancelled"], 1)
        reply = send_mock.call_args.args[1]
        self.assertIn("Cancelled", reply)
        self.assertIn(REF, reply)
        self.assertEqual(
            client.patch.call_args.kwargs["json"], {"status": "processed"})

    async def test_reviewer_cancel_attempt_refused(self):
        outcome, summary, _, cancel_mock, send_mock = \
            await self._run_cancel("CANCEL %s" % REF,
                cancel_error=human_confirmation.ReviewUnauthorizedError(
                    "UNAUTHORIZED: only the reporter can cancel this draft"))
        self.assertEqual(outcome["outcome"], "refused")
        self.assertEqual(outcome["error"], "ReviewUnauthorizedError")
        self.assertEqual(summary["reviews_refused"], 1)
        cancel_mock.assert_called_once()
        reply = send_mock.call_args.args[1]
        self.assertIn("Refused", reply)

    async def test_cancel_terminal_row_refused(self):
        outcome, summary, _, _, _ = await self._run_cancel(
            "CANCEL %s" % REF,
            cancel_error=human_confirmation.NotReviewableError(
                "NOT_REVIEWABLE: submission has status cancelled"))
        self.assertEqual(outcome["outcome"], "refused")
        self.assertEqual(summary["reviews_refused"], 1)

    async def test_cancel_malformed_text_is_not_a_command(self):
        # "CANCEL" without a reference is ordinary text: no draft, help.
        outcome, summary, client, cancel_mock, _ = \
            await self._run_cancel("CANCEL everything please",
                cancel_result=self._cancelled())
        cancel_mock.assert_not_called()
        self.assertEqual(outcome["outcome"], "help")
        submissions = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(submissions, [])

    async def test_cancel_wiring_calls_exactly_one_rpc(self):
        seen = []

        class FakeClient:
            async def post(self, path, json=None, params=None, headers=None):
                seen.append((path, json))
                return httpx.Response(200, json={
                    "status": "cancelled", "review_action": "cancelled",
                    "submission_kind": "expense",
                    "submission_id": SUBMISSION_UUID, "review_ref": REF,
                    "request_key": "cancel:inbox-1", "audit_id": "a7",
                    "reason": None, "is_retry": False})

        result = await review_service_module.cancel_from_chat(
            FakeClient(), REF, "telegram", CHAT_ID, "cancel:inbox-1")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(seen), 1)
        path, body = seen[0]
        self.assertEqual(path,
            "/rest/v1/rpc/amose_cancel_submission")
        self.assertEqual(set(body),
            {"p_review_ref", "p_requester_provider", "p_requester_sender",
                "p_request_key", "p_reason"})
        self.assertIsNone(body["p_reason"])


class MappingSyncTests(unittest.TestCase):
    KINDS = ("production", "poultry_daily_report", "sale", "payment",
        "expense", "cash_handover", "bank_deposit", "stock",
        "customer_payment", "customer_debt")

    def test_review_and_posting_mappings_cover_every_kind(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, human_confirmation.KIND_TO_POSTING)
                self.assertIn(kind, operational_posting.KIND_TO_POSTING)
                posting, rpc = operational_posting.posting_for_kind(kind)
                self.assertIn(rpc, operational_posting.RPC_ALLOWLIST)
                self.assertIn(kind, review_service_module.REVIEWABLE_KINDS)

    def test_command_confirm_validation_accepts_every_kind(self):
        for kind, result in CONFIRMED_RESULTS.items():
            with self.subTest(kind=kind):
                review_service_module._validate_command_confirm_result(
                    dict(result), REF, "k1")

    def test_validate_verified_accepts_new_kinds(self):
        checked = human_confirmation.validate_verified("stock", {
            "product_id": PRODUCT_UUID, "normal_quantity": 120,
            "cold_quantity": 75})
        self.assertEqual(checked["posting_type"], "stock")
        checked = human_confirmation.validate_verified("customer_payment", {
            "customer_id": CUSTOMER_UUID, "amount_kobo": 2500000,
            "method": "transfer"})
        self.assertEqual(checked["posting_type"], "customer_payment")
        checked = human_confirmation.validate_verified("customer_debt", {
            "customer_name": "Ada", "amount_kobo": 1200000})
        self.assertEqual(checked["posting_type"], "customer_debt")

    def test_validate_verified_rejects_bad_new_kind_blocks(self):
        with self.assertRaises(human_confirmation.MalformedVerifiedError):
            human_confirmation.validate_verified("stock", {
                "product_id": PRODUCT_UUID, "normal_quantity": -1,
                "cold_quantity": 75})
        with self.assertRaises(human_confirmation.MalformedVerifiedError):
            human_confirmation.validate_verified("customer_payment", {
                "customer_id": CUSTOMER_UUID, "amount_kobo": 0,
                "method": "transfer"})
        with self.assertRaises(human_confirmation.MalformedVerifiedError):
            human_confirmation.validate_verified("customer_debt", {
                "amount_kobo": 1200000})
        with self.assertRaises(human_confirmation.UnsupportedKindError):
            human_confirmation.validate_verified("telegram_intake", {})

    def test_posting_validation_rejects_incomplete_results(self):
        with self.assertRaises(
                operational_posting.PostingDatabaseError):
            operational_posting._validate_rpc_result(
                {"status": "posted", "posting_type": "stock",
                    "submission_id": SUBMISSION_UUID},
                "stock", SUBMISSION_UUID)

    def test_cancel_result_validation(self):
        good = {"status": "cancelled", "review_action": "cancelled",
            "submission_kind": "expense",
            "submission_id": SUBMISSION_UUID, "review_ref": REF,
            "request_key": "cancel:inbox-1", "audit_id": "a7",
            "reason": "filed twice", "is_retry": False}
        human_confirmation._validate_cancel_result(
            dict(good), REF, "cancel:inbox-1", "filed twice")
        bad = dict(good, status="rejected")
        with self.assertRaises(human_confirmation.WorkflowDatabaseError):
            human_confirmation._validate_cancel_result(
                bad, REF, "cancel:inbox-1", "filed twice")
        bad = dict(good, submission_kind="telegram_intake")
        with self.assertRaises(human_confirmation.WorkflowDatabaseError):
            human_confirmation._validate_cancel_result(
                bad, REF, "cancel:inbox-1", "filed twice")

class TransplantFidelityTests(unittest.TestCase):
    """Applied migrations are forward-only (never edited), so the new
    migration's transplanted bodies can be pinned against them: only the
    intended gates/branches may differ."""

    def _bodies(self, name):
        text = (pathlib.Path(__file__).parent / "supabase" / "migrations"
            / name).read_text(encoding="utf-8")
        text = text.replace("\r\n", "\n")
        out = {}
        for part in text.split("create or replace function public.")[1:]:
            func = part.split("(", 1)[0]
            out.setdefault(func, part.split("end;\n$func$;", 1)[0])
        return out

    def test_queue_transplants_differ_only_in_kind_gate(self):
        import difflib
        daily = self._bodies("20260920000000_daily_business_records.sql")
        tg = self._bodies("20260918155717_telegram_review_backup.sql")
        new = self._bodies("20260922000000_telegram_full_intake.sql")
        for func, old in (
                ("amose_queue_review_requests",
                    daily["amose_queue_review_requests"]),
                ("amose_queue_telegram_review_requests",
                    tg["amose_queue_telegram_review_requests"])):
            diff = [line for line in difflib.unified_diff(
                old.splitlines(), new[func].splitlines(), lineterm="")
                if line[:1] in "+-" and line[1:3] != line[:1] * 2]
            self.assertEqual(len(diff), 3, diff)

    def test_review_text_transplant_only_adds_kinds_and_suffix(self):
        import difflib
        daily = self._bodies("20260920000000_daily_business_records.sql")
        new = self._bodies("20260922000000_telegram_full_intake.sql")
        diff = [line for line in difflib.unified_diff(
            daily["_amose_review_request_text"].splitlines(),
            new["_amose_review_request_text"].splitlines(), lineterm="")
            if line[:1] in "+-" and line[1:3] != line[:1] * 2]
        for snippet in ("Sale proposal: ", "Production proposal: good ",
                "Payment proposal: amount ", "Expense proposal: ",
                "Cash handover proposal: amount ",
                "Bank deposit proposal: amount "):
            self.assertIn(snippet, new["_amose_review_request_text"])
        removed = [line for line in diff if line.startswith("-")]
        for line in removed:
            lowered = line.lower()
            self.assertTrue("sale" in lowered or "api review" in lowered
                or "review " in lowered or "your-unique-key" in lowered
                or "p_branch_id, 64" in lowered
                or lowered.strip("- ") in ("else", "end if;"), line)

    def test_confirm_transplant_only_adds_mapping_and_branches(self):
        import difflib
        daily = self._bodies("20260920000000_daily_business_records.sql")
        new = self._bodies("20260922000000_telegram_full_intake.sql")
        diff = [line for line in difflib.unified_diff(
            daily["amose_confirm_submission"].splitlines(),
            new["amose_confirm_submission"].splitlines(), lineterm="")
            if line[:1] in "+-" and line[1:3] != line[:1] * 2]
        for line in diff:
            lowered = line.lower()
            self.assertTrue(
                any(token in lowered for token in ("stock",
                    "customer_payment", "customer_debt",
                    "validate_verified",
                    "amose_post_stock", "amose_post_customer"))
                or lowered.strip("+- ") in ("else", "end if;"),
                line)

    def test_chat_confirm_keeps_exact_sale_rules(self):
        new = self._bodies("20260922000000_telegram_full_intake.sql")
        body = new["amose_review_confirm_command"]
        for snippet in ("parsed sale amounts are not whole numbers",
                "no single active product matches the reported unit",
                "'storage_state', 'normal'",
                "for update of s"):
            self.assertIn(snippet, body)


class Migration23Tests(unittest.TestCase):
    PATH = pathlib.Path(__file__).parent / "supabase" / "migrations" \
        / "20260923000000_intake_correction_cancel.sql"

    def _sql(self):
        return self.PATH.read_text(encoding="utf-8")

    def test_migration_exists_and_commits_once(self):
        self.assertTrue(self.PATH.exists())
        text = self._sql()
        self.assertIn("begin;", text)
        self.assertTrue(text.rstrip().endswith("commit;"))
        self.assertEqual(text.count("begin;"), 1)

    def test_all_functions_secured(self):
        text = self._sql()
        self.assertEqual(text.count("security definer"), 6)
        self.assertEqual(text.count("set search_path = pg_catalog"), 6)

    def test_cancelled_status_everywhere(self):
        text = self._sql()
        self.assertIn("'draft', 'confirmed', 'rejected', 'cancelled'",
            text)
        self.assertIn("status in ('open', 'confirmed', 'rejected',"
            " 'cancelled')", text)
        self.assertIn("action in ('corrected', 'confirmed', 'rejected',"
            " 'cancelled')", text)
        self.assertIn("'review_confirmed', 'review_rejected',", text)
        self.assertIn("'review_cancelled'", text)

    def test_cancel_rpc_and_helpers_secured(self):
        text = self._sql()
        head = "create function public.amose_cancel_submission("
        self.assertIn(head, text)
        self.assertIn("security definer", text[
            text.index(head):text.index(head) + 600])
        self.assertIn("set search_path = pg_catalog", text[
            text.index(head):text.index(head) + 600])
        head = "revoke execute on function public.amose_cancel_submission("
        self.assertIn(head, text)
        self.assertIn("from public, anon, authenticated;",
            text[text.index(head):text.index(head) + 300])
        head = "grant execute on function public.amose_cancel_submission("
        self.assertIn(head, text)
        self.assertIn("to service_role",
            text[text.index(head):text.index(head) + 300])
        for helper in (
                "_amose_apply_intake_corrections(text, jsonb, jsonb)",):
            head = "revoke execute on function public.%s" % helper
            self.assertIn(head, text)
            self.assertIn("from public, anon, authenticated, service_role;",
                text[text.index(head):text.index(head) + 300])
        # Old 5-arg chat-confirm signature is dropped so no stale
        # builder survives; the 6-arg form is service-role only.
        self.assertIn("drop function if exists\n"
            "  public.amose_review_confirm_command(text, text, text,"
            " text, text);", text)
        head = "revoke execute on function public." \
            "amose_review_confirm_command(\n" \
            "  text, text, text, text, text, jsonb)"
        self.assertIn(head, text)

    def test_chat_confirm_transplant_only_adds_corrections(self):
        import difflib

        def bodies(name):
            text = (pathlib.Path(__file__).parent / "supabase"
                / "migrations" / name).read_text(encoding="utf-8")
            text = text.replace("\r\n", "\n")
            out = {}
            for part in text.split(
                    "create or replace function public.")[1:]:
                func = part.split("(", 1)[0]
                out.setdefault(func, part.split("end;\n$func$;", 1)[0])
            return out

        old = bodies("20260922000000_telegram_full_intake.sql")
        new = bodies("20260923000000_intake_correction_cancel.sql")
        hunks, current = [], []
        for line in difflib.unified_diff(
                old["amose_review_confirm_command"].splitlines(),
                new["amose_review_confirm_command"].splitlines(),
                lineterm=""):
            if line.startswith(("---", "+++")):
                continue
            if line.startswith("@@"):
                if current:
                    hunks.append(current)
                current = []
            else:
                current.append(line)
        if current:
            hunks.append(current)
        # Three surgical hunks only: signature, declaration, and the
        # corrections application before the kind builders. Every hunk
        # must carry correction content; the only removed line is the
        # old signature tail; added structural closers belong to the
        # corrections block itself.
        self.assertEqual(len(hunks), 3)
        removed = []
        for hunk in hunks:
            added = [line[1:] for line in hunk
                if line.startswith("+") and not line.startswith("+++")
                and line[1:].strip()]
            self.assertTrue(added)
            self.assertTrue(
                any("orrection" in line.lower() for line in added),
                hunk)
            # Runs of consecutive added comment lines that explain
            # correction behavior are accepted as a block when at
            # least one line names it; executable lines below stay
            # restricted to the intended block.
            runs, run = [], []
            for line in added:
                if line.strip().startswith("--"):
                    run.append(line)
                else:
                    if run:
                        runs.append(run)
                    run = []
            if run:
                runs.append(run)
            explained = set()
            for run in runs:
                if any("correct" in line.lower() for line in run):
                    explained.update(run)
            for line in added:
                lowered = line.lower()
                if "orrection" in lowered or line in explained:
                    continue
                # Only the corrections block's own reason guard and
                # its closers may lack the word itself.
                self.assertIn(line.strip(), (
                    "end if;", "if v_reason is null then"), hunk)
            removed.extend(
                line[1:] for line in hunk
                if line.startswith("-") and not line.startswith("---")
                and line[1:].strip())
        self.assertEqual(len(removed), 1)
        self.assertIn("p_correction_reason text default null",
            removed[0])
        qhunks, current = [], []
        for line in difflib.unified_diff(
                old["amose_queue_review_requests"].splitlines(),
                new["amose_queue_review_requests"].splitlines(),
                lineterm=""):
            if line.startswith(("---", "+++")):
                continue
            if line.startswith("@@"):
                if current:
                    qhunks.append(current)
                current = []
            else:
                current.append(line)
        if current:
            qhunks.append(current)
        # The queue transplant extends the lock row with the origin
        # provider (s.provider/v_provider) for the cross-channel guard
        # and reworks routing around it. Every changed hunk must stay
        # anchored to that guard: at least one added guard-token line,
        # so edits elsewhere (kinds, amounts, outcomes) still fail.
        # Removed executable lines must survive inside an added line of
        # the same hunk: columns may be added, never lost. Removed
        # comments are non-functional.
        self.assertTrue(qhunks)
        for hunk in qhunks:
            added = [line[1:] for line in hunk
                if line.startswith("+") and not line.startswith("+++")
                and line[1:].strip()]
            removed = [line[1:] for line in hunk
                if line.startswith("-") and not line.startswith("---")
                and line[1:].strip()]
            self.assertTrue(added, hunk)
            self.assertTrue(
                any(any(token in line.lower()
                    for token in ("provider", "v_acct", "whatsapp",
                        "snapshot", "scope"))
                    for line in added),
                hunk)
            runs, run = [], []
            for line in added:
                if line.strip().startswith("--"):
                    run.append(line)
                else:
                    if run:
                        runs.append(run)
                    run = []
            if run:
                runs.append(run)
            explained = set()
            for run in runs:
                if any("snapshot" in line.lower()
                        or "whatsapp" in line.lower()
                        or "scope" in line.lower()
                        or "provider" in line.lower()
                        for line in run):
                    explained.update(run)
            for line in removed:
                if line.strip().startswith("--"):
                    continue
                lowered = line.lower()
                if any(token in lowered for token in ("provider",
                        "v_acct", "whatsapp", "snapshot", "scope")):
                    continue
                self.assertTrue(
                    any(line.strip() in entry for entry in added),
                    hunk)
            for line in added:
                lowered = line.lower()
                if any(token in lowered for token in ("provider",
                        "v_acct", "whatsapp", "snapshot", "scope")):
                    continue
                if line.strip().startswith("--"):
                    self.assertIn(line, explained, hunk)
                elif line.strip() in ("else", "end if;", "begin",
                        "end;"):
                    continue
                else:
                    # Only the guard's own plain-SQL scope filter may
                    # appear without a token; anything else fails.
                    self.assertIn(line.strip(), (
                        "where a.tenant_id = v_tenant"
                        " and a.business_id = v_business",
                        "and a.enabled = true;"), hunk)


class MigrationSecurityTests(unittest.TestCase):
    PATH = pathlib.Path(__file__).parent / "supabase" / "migrations" \
        / "20260922000000_telegram_full_intake.sql"

    def _sql(self):
        return self.PATH.read_text(encoding="utf-8")

    def test_migration_exists_and_commits(self):
        self.assertTrue(self.PATH.exists())
        text = self._sql()
        self.assertTrue(text.startswith("--"))
        self.assertIn("begin;", text)
        self.assertTrue(text.rstrip().endswith("commit;"))

    def test_new_tables_are_locked_down(self):
        text = self._sql()
        for table in ("biz_stock_counts", "biz_customer_debts",
                "biz_customer_payments"):
            with self.subTest(table=table):
                self.assertIn(
                    "create table public.%s (" % table, text)
                self.assertIn(
                    "alter table public.%s enable row level security"
                    % table, text)
        self.assertIn("revoke all on public.biz_stock_counts", text)
        self.assertIn(
            "grant select, insert on public.biz_stock_counts to service_role",
            text)
        self.assertIn(
            "grant select, insert, update on public.biz_customer_debts "
            "to service_role", text)
        self.assertIn(
            "grant select, insert, update on public.biz_customer_payments "
            "to service_role", text)
        self.assertIn(
            "revoke truncate on public.biz_stock_counts from service_role",
            text)
        self.assertNotIn(" to anon", text)
        self.assertNotIn(" to authenticated", text)

    def test_rpcs_are_secured_service_role_only(self):
        text = self._sql()
        for rpc in ("amose_post_stock(uuid)",
                "amose_post_customer_debt(uuid)",
                "amose_post_customer_payment(uuid)",
                "amose_review_confirm_command(text, text, text, text, text)"):
            with self.subTest(rpc=rpc):
                head = "revoke execute on function public.%s" % rpc
                self.assertIn(head, text)
                self.assertIn("from public, anon, authenticated;",
                    text[text.index(head):text.index(head) + 400])
                head = "grant execute on function public.%s" % rpc
                self.assertIn(head, text)
                self.assertIn("to service_role",
                    text[text.index(head):text.index(head) + 400])
        for helper in (
                "_amose_resolve_intake_customer(uuid, text, text, text)",
                "_amose_resolve_intake_employee(uuid, text, text, text)",
                "_amose_resolve_intake_product(uuid, text)",
                "_amose_validate_verified_intake(text, text, jsonb)"):
            with self.subTest(helper=helper):
                self.assertIn(
                    "revoke execute on function public.%s" % helper
                    + "\n  from public, anon, authenticated, service_role;",
                    text)
        self.assertEqual(text.count("security definer"), 12)
        self.assertEqual(text.count("set search_path = pg_catalog"), 12)

    def test_every_kind_wired_in_every_gate(self):
        text = self._sql()
        gates = [line for line in text.splitlines()
            if "v_kind not in (" in line]
        self.assertEqual(len(gates), 2)
        for gate in gates:
            for kind in ("production", "poultry_daily_report", "sale",
                    "payment", "expense", "cash_handover", "bank_deposit",
                    "stock", "customer_payment", "customer_debt"):
                self.assertIn("'%s'" % kind, text[
                    text.index(gate):text.index(gate) + 400])
        for kind in ("stock", "customer_payment", "customer_debt"):
            self.assertIn("v_kind = '%s'" % kind, text)
            self.assertIn("v_posting = '%s'" % kind, text)
            self.assertIn("p_posting_type = '%s'" % kind, text)
            self.assertIn("p_kind = '%s'" % kind, text)
        for posting in ("'stock', 'customer_payment', 'customer_debt'"):
            self.assertIn(posting, text)

    def test_python_mappings_match_migration_gates(self):
        text = self._sql()
        for kind in human_confirmation.KIND_TO_POSTING:
            with self.subTest(kind=kind):
                self.assertIn("'%s'" % kind, text)
        for kind, (posting, rpc) in operational_posting.KIND_TO_POSTING.items():
            with self.subTest(kind=kind):
                self.assertIn(rpc, text)


if __name__ == "__main__":
    unittest.main()
