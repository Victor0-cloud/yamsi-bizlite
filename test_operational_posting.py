"""Phase 2 tests: the controlled operational posting layer.

Unit tests use the same mocking conventions as the rest of the suite
(patch httpx.AsyncClient with an AsyncMock client; real httpx.Response
objects for payloads). No test touches a live database and no test invents
business IDs beyond local throwaway fixtures.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx

import operational_posting
from operational_posting import (
    ApprovalRequiredError,
    IncompletePostingError,
    MalformedSubmissionError,
    PostingDatabaseError,
    PostingError,
    ScopeMismatchError,
    SubmissionNotFoundError,
    UnconfirmedSubmissionError,
    UnsupportedKindError,
    post_submission,
    posting_for_kind,
)

SUBMISSION_ID = "11111111-1111-1111-1111-111111111111"


def _submission_row(kind="sale", status="confirmed"):
    return {"id": SUBMISSION_ID, "tenant_id": "tenant-1",
        "business_id": "amose_table_water", "branch_id": "asaba",
        "employee_id": "emp-1", "kind": kind, "status": status}


def _client(get_rows=None, post_response=None, get_status=200, post_status=201):
    client = AsyncMock()
    client.get = AsyncMock(return_value=httpx.Response(get_status, json=get_rows or []))
    client.post = AsyncMock(return_value=post_response)
    return client


def _run_with_client(client, coro):
    with patch("operational_posting.credentials",
               return_value=("https://example.supabase.co", "test-key")), \
         patch("operational_posting.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        return asyncio.run(coro), client


_OK_IDS = {
    "production": {"production_run_id": "run-1"},
    "sale": {"sale_id": "sale-1"},
    "payment": {"payment_id": "pay-1"},
    "expense": {"expense_id": "exp-1"},
}


def _ok_result(posting_type="sale"):
    body = {"status": "posted", "posting_type": posting_type,
        "submission_id": SUBMISSION_ID, "is_retry": False,
        "brain_memory_id": "mem-1"}
    body.update(_OK_IDS[posting_type])
    return httpx.Response(200, json=body)


class KindMappingTests(unittest.TestCase):
    def test_supported_kinds_map_to_allowlisted_rpcs(self):
        self.assertEqual(posting_for_kind("production"), ("production", "amose_post_production"))
        self.assertEqual(posting_for_kind("poultry_daily_report"), ("production", "amose_post_production"))
        self.assertEqual(posting_for_kind("sale"), ("sale", "amose_post_sale"))
        self.assertEqual(posting_for_kind("payment"), ("payment", "amose_post_payment"))
        self.assertEqual(posting_for_kind("expense"), ("expense", "amose_post_expense"))

    def test_unsupported_kind_rejected_without_database(self):
        for kind in ("whatsapp_message", "poultry_report_v9", None, "", "DROP TABLE biz_sales;"):
            with self.assertRaises(UnsupportedKindError):
                posting_for_kind(kind)

    def test_no_arbitrary_function_name_accepted(self):
        client = _client()
        with patch("operational_posting.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("operational_posting.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(PostingError):
                asyncio.run(operational_posting._call_rpc("amose_post_salary; DROP TABLE x", SUBMISSION_ID))
            with self.assertRaises(PostingError):
                asyncio.run(operational_posting._call_rpc("public.amose_post_sale", SUBMISSION_ID))
        client.post.assert_not_called()


class UnconfirmedSubmissionTests(unittest.TestCase):
    def test_draft_submission_rejected(self):
        client = _client(get_rows=[_submission_row(status="draft")],
            post_response=_ok_result())
        with self.assertRaises(UnconfirmedSubmissionError):
            _run_with_client(client, post_submission(SUBMISSION_ID))
        client.post.assert_not_called()

    def test_pending_rejected_voided_submissions_rejected(self):
        for status in ("pending", "rejected", "voided"):
            client = _client(get_rows=[_submission_row(status=status)],
                post_response=_ok_result())
            with self.assertRaises(UnconfirmedSubmissionError, msg=status):
                _run_with_client(client, post_submission(SUBMISSION_ID))
            client.post.assert_not_called()

    def test_missing_submission_rejected(self):
        client = _client(get_rows=[], post_response=_ok_result())
        with self.assertRaises(SubmissionNotFoundError):
            _run_with_client(client, post_submission(SUBMISSION_ID))
        client.post.assert_not_called()

    def test_unsupported_kind_creates_no_records(self):
        client = _client(get_rows=[_submission_row(kind="whatsapp_message")],
            post_response=_ok_result())
        with self.assertRaises(UnsupportedKindError):
            _run_with_client(client, post_submission(SUBMISSION_ID))
        client.post.assert_not_called()

    def test_blank_submission_id_rejected(self):
        for bad in ("", None, 123):
            with self.assertRaises(PostingError):
                asyncio.run(post_submission(bad))


class RpcSelectionTests(unittest.TestCase):
    def _selected_rpc(self, kind):
        posting_type, _rpc = posting_for_kind(kind)
        client = _client(get_rows=[_submission_row(kind=kind)],
            post_response=_ok_result(posting_type))
        _run_with_client(client, post_submission(SUBMISSION_ID))
        rpc_path = client.post.call_args.args[0]
        return rpc_path, client

    def test_sale_selects_sale_rpc(self):
        path, _client_used = self._selected_rpc("sale")
        self.assertEqual(path, "/rest/v1/rpc/amose_post_sale")

    def test_production_selects_production_rpc(self):
        path, _client_used = self._selected_rpc("production")
        self.assertEqual(path, "/rest/v1/rpc/amose_post_production")

    def test_poultry_report_selects_production_rpc(self):
        path, _client_used = self._selected_rpc("poultry_daily_report")
        self.assertEqual(path, "/rest/v1/rpc/amose_post_production")

    def test_payment_selects_payment_rpc(self):
        path, _client_used = self._selected_rpc("payment")
        self.assertEqual(path, "/rest/v1/rpc/amose_post_payment")

    def test_expense_selects_expense_rpc(self):
        path, _client_used = self._selected_rpc("expense")
        self.assertEqual(path, "/rest/v1/rpc/amose_post_expense")

    def test_rpc_receives_only_the_submission_id(self):
        _path, client = self._selected_rpc("sale")
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body, {"p_submission_id": SUBMISSION_ID})


class MalformedSubmissionTests(unittest.TestCase):
    def test_malformed_rpc_error_maps_to_typed_error(self):
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(400, json={"message": "MALFORMED: submission x has no verified sale block"}),
            post_status=400)
        with self.assertRaises(MalformedSubmissionError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_scope_error_maps_to_typed_error(self):
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(400, json={"message": "SCOPE: product x is not active"}),
            post_status=400)
        with self.assertRaises(ScopeMismatchError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_approval_error_maps_to_typed_error(self):
        client = _client(get_rows=[_submission_row(kind="expense")],
            post_response=httpx.Response(400, json={"message": "APPROVAL: approval request x is not approved"}),
            post_status=400)
        with self.assertRaises(ApprovalRequiredError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_unconfirmed_rpc_error_maps_to_typed_error(self):
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(400, json={"message": "UNCONFIRMED: submission x has status draft"}),
            post_status=400)
        with self.assertRaises(UnconfirmedSubmissionError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_unknown_rpc_failure_is_posting_database_error(self):
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(500, json={"message": "something unexpected"}),
            post_status=500)
        with self.assertRaises(PostingDatabaseError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_unexpected_result_shape_rejected(self):
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(200, json=["not", "a", "dict"]))
        with self.assertRaises(PostingDatabaseError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_http_failure_is_posting_database_error(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=httpx.Response(200, json=[_submission_row()]))
        client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch("operational_posting.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("operational_posting.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(PostingDatabaseError):
                asyncio.run(post_submission(SUBMISSION_ID))


class IdempotencyTests(unittest.TestCase):
    def test_retry_returns_existing_result_without_duplicate_behavior(self):
        existing = {"status": "already_posted", "posting_type": "sale",
            "submission_id": SUBMISSION_ID, "sale_id": "sale-1",
            "brain_memory_id": "mem-1", "is_retry": True}
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(200, json=existing))
        result, client = _run_with_client(client, post_submission(SUBMISSION_ID))
        self.assertEqual(result, existing)
        self.assertTrue(result["is_retry"])
        # Exactly one RPC call -- no follow-up writes, no re-post.
        client.post.assert_called_once()

    def test_fresh_post_returns_posted_result(self):
        posted = {"status": "posted", "posting_type": "production",
            "submission_id": SUBMISSION_ID, "production_run_id": "run-1",
            "brain_memory_id": "mem-1", "is_retry": False}
        client = _client(get_rows=[_submission_row(kind="production")],
            post_response=httpx.Response(200, json=posted))
        result, _client_used = _run_with_client(client, post_submission(SUBMISSION_ID))
        self.assertEqual(result["status"], "posted")
        self.assertFalse(result["is_retry"])


class AtomicityBoundaryTests(unittest.TestCase):
    def test_brain_memory_is_inside_the_rpc_not_a_python_write(self):
        """Posting performs exactly one RPC call and zero direct REST writes
        to operational or Brain tables -- memory is part of the atomic
        database operation, not a later independent Python write."""
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=_ok_result())
        _run_with_client(client, post_submission(SUBMISSION_ID))
        client.post.assert_called_once()
        rpc_path = client.post.call_args.args[0]
        self.assertTrue(rpc_path.startswith("/rest/v1/rpc/"))
        # The submission read is the only GET; nothing else is written.
        for call in client.post.call_args_list:
            self.assertTrue(call.args[0].startswith("/rest/v1/rpc/"))

    def test_scope_comes_from_authoritative_submission_row(self):
        """The RPC body carries only the submission ID; tenant/business/
        branch identifiers are read from the submission row itself, never
        accepted as caller arguments (post_submission takes no scope args)."""
        import inspect
        params = list(inspect.signature(post_submission).parameters)
        self.assertEqual(params, ["submission_id"])
        client = _client(get_rows=[_submission_row(kind="expense")],
            post_response=_ok_result("expense"))
        result, client = _run_with_client(client, post_submission(SUBMISSION_ID))
        self.assertEqual(result["posting_type"], "expense")
        self.assertEqual(client.post.call_args.kwargs["json"],
            {"p_submission_id": SUBMISSION_ID})


class NoIngestionPostingTests(unittest.TestCase):
    def test_whatsapp_ingestion_never_posts_operational_data(self):
        """Ordinary inbox processing (receipt -> draft submission) makes no
        operational-posting call, even for a fully-parsed sale message."""
        import message_processor
        inbox_rows = [{"id": "inbox-1", "provider": "whatsapp", "provider_account": "acct-1",
            "payload": {"kind": "message", "event": {
                "from": "+12025550123", "type": "text",
                "text": {"body": "Sold 50 bags at 500 NGN"}}}}]
        client = AsyncMock()

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_message_inbox":
                return httpx.Response(200, json=inbox_rows)
            if path == "/rest/v1/biz_sender_identities":
                return httpx.Response(200, json=[{"tenant_id": "t-1", "employee_id": "emp-1"}])
            if path == "/rest/v1/biz_assignments":
                return httpx.Response(200, json=[{"business_id": "b-1", "branch_id": "br-1"}])
            if path == "/rest/v1/biz_submissions":
                return httpx.Response(200, json=[{"id": "sub-1"}])
            raise AssertionError("unexpected GET " + path)

        client.get = AsyncMock(side_effect=fake_get)
        client.post = AsyncMock(return_value=httpx.Response(201))
        client.patch = AsyncMock(return_value=httpx.Response(204))
        with patch("message_processor.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("message_processor.httpx.AsyncClient") as factory, \
             patch("operational_posting.post_submission", new_callable=AsyncMock) as post:
            factory.return_value.__aenter__.return_value = client
            summary = asyncio.run(message_processor.process_inbox_batch())
        self.assertEqual(summary["submitted"], 1)
        post.assert_not_called()
        # Belt and braces: ingestion must not even import the posting layer.
        self.assertNotIn("operational_posting", dir(message_processor))


class HelperNotCallableTests(unittest.TestCase):
    def test_helper_is_not_allowlisted(self):
        self.assertNotIn("_amose_lock_confirmed_submission",
                         operational_posting.RPC_ALLOWLIST)
        for kind in ("production", "poultry_daily_report", "sale",
                     "payment", "expense"):
            _posting_type, rpc = posting_for_kind(kind)
            self.assertNotEqual(rpc, "_amose_lock_confirmed_submission")
            self.assertFalse(rpc.startswith("_amose"))

    def test_helper_call_refused_before_network(self):
        client = _client()
        with patch("operational_posting.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("operational_posting.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(PostingError):
                asyncio.run(operational_posting._call_rpc(
                    "_amose_lock_confirmed_submission", SUBMISSION_ID))
        client.post.assert_not_called()


class StrictResponseTests(unittest.TestCase):
    def _post_with_body(self, kind, body, status=200):
        client = _client(get_rows=[_submission_row(kind=kind)],
            post_response=httpx.Response(status, json=body), post_status=status)
        return _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_wrong_posting_type_rejected(self):
        body = {"status": "posted", "posting_type": "payment",
            "submission_id": SUBMISSION_ID, "is_retry": False,
            "payment_id": "pay-1", "brain_memory_id": "mem-1"}
        with self.assertRaises(PostingDatabaseError):
            self._post_with_body("sale", body)

    def test_missing_required_id_rejected(self):
        body = {"status": "posted", "posting_type": "sale",
            "submission_id": SUBMISSION_ID, "is_retry": False,
            "brain_memory_id": "mem-1"}
        with self.assertRaises(PostingDatabaseError):
            self._post_with_body("sale", body)

    def test_empty_required_id_rejected(self):
        body = {"status": "posted", "posting_type": "sale",
            "submission_id": SUBMISSION_ID, "is_retry": False,
            "sale_id": "", "brain_memory_id": "mem-1"}
        with self.assertRaises(PostingDatabaseError):
            self._post_with_body("sale", body)

    def test_unexpected_status_rejected(self):
        body = {"status": "pending", "posting_type": "sale",
            "submission_id": SUBMISSION_ID, "is_retry": False,
            "sale_id": "sale-1", "brain_memory_id": "mem-1"}
        with self.assertRaises(PostingDatabaseError):
            self._post_with_body("sale", body)

    def test_submission_id_mismatch_rejected(self):
        body = {"status": "posted", "posting_type": "sale",
            "submission_id": "22222222-2222-2222-2222-222222222222",
            "is_retry": False, "sale_id": "sale-1", "brain_memory_id": "mem-1"}
        with self.assertRaises(PostingDatabaseError):
            self._post_with_body("sale", body)

    def test_incomplete_retry_maps_to_typed_error(self):
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(400, json={
                "message": "INCOMPLETE: submission x sale posting is missing its brain memory"}),
            post_status=400)
        with self.assertRaises(IncompletePostingError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_incomplete_is_a_posting_error(self):
        self.assertTrue(issubclass(IncompletePostingError, PostingError))


class CredentialsFailureTests(unittest.TestCase):
    def test_missing_configuration_is_a_posting_error(self):
        from supabase_backend import DatabaseUnavailable
        with patch("operational_posting.credentials",
                   side_effect=DatabaseUnavailable("Supabase server key is missing.")), \
             patch("operational_posting.httpx.AsyncClient") as factory:
            with self.assertRaises(PostingError):
                asyncio.run(post_submission(SUBMISSION_ID))
            factory.assert_not_called()

    def test_configuration_failure_before_rpc_is_a_posting_error(self):
        from supabase_backend import DatabaseUnavailable
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=_ok_result())
        calls = {"n": 0}

        def flaky_credentials():
            calls["n"] += 1
            if calls["n"] == 1:
                return ("https://example.supabase.co", "test-key")
            raise DatabaseUnavailable("Supabase server key is missing.")

        with patch("operational_posting.credentials",
                   side_effect=flaky_credentials), \
             patch("operational_posting.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(PostingError):
                asyncio.run(post_submission(SUBMISSION_ID))
        client.post.assert_not_called()


class UnverifiedBoundaryTests(unittest.TestCase):
    def test_top_level_unverified_values_never_leave_python(self):
        """A submission row stuffed with top-level decoy fields (a hostile
        product_id, totals, status) still produces an RPC body carrying only
        the submission ID: scope and facts come from the locked row and the
        verified block inside the database, never from top-level values."""
        row = _submission_row(kind="sale")
        row.update({"product_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "total_kobo": 1, "status": "confirmed",
            "tenant_id": "attacker-tenant", "branch_id": "attacker-branch"})
        client = _client(get_rows=[row], post_response=_ok_result())
        _run_with_client(client, post_submission(SUBMISSION_ID))
        self.assertEqual(client.post.call_args.kwargs["json"],
            {"p_submission_id": SUBMISSION_ID})

    def test_mismatched_verified_kind_surfaces_as_malformed(self):
        """True kind-agreement enforcement lives in the RPCs (local SQL
        probes); here we prove the Python layer surfaces that refusal
        as a typed MalformedSubmissionError rather than trusting output."""
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(400, json={
                "message": "MALFORMED: submission x verified kind payment "
                           "does not match submission kind sale"}),
            post_status=400)
        with self.assertRaises(MalformedSubmissionError):
            _run_with_client(client, post_submission(SUBMISSION_ID))

    def test_embedded_overpayment_refusal_surfaces_typed(self):
        """Overpayment enforcement lives in the RPCs (local SQL probes);
        the Python layer must surface the refusal without creating anything
        further (exactly one RPC call, error propagates)."""
        client = _client(get_rows=[_submission_row(kind="sale")],
            post_response=httpx.Response(400, json={
                "message": "MALFORMED: submission x embedded payment 900 "
                           "exceeds sale total 800"}),
            post_status=400)
        with self.assertRaises(MalformedSubmissionError):
            _run_with_client(client, post_submission(SUBMISSION_ID))
        client.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
