"""Phase 4 tests: secure WhatsApp review integration.

Covers the administrative authorization boundary, the review-request queue
trigger, and strict inbound REVIEW command handling -- all with mocked
transport (AsyncMock httpx clients, real httpx.Response payloads). No test
touches a live database. Database-enforced properties are additionally
covered by the transactional SQL probes (rollback battery) plus the live
automated concurrency run.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx

import message_processor
import review_service
from review_service import (
    AuthorizationAdminError,
    AuthorizationNotFoundError,
    ReviewServiceError,
    confirm_from_chat,
    grant_reviewer,
    handle_review_command,
    list_reviewers,
    queue_review_requests,
    reject_from_chat,
    revoke_reviewer,
)
from human_confirmation import (
    RequestConflictError,
    ReviewReferenceError,
    ReviewUnauthorizedError,
    ReviewValidationError,
    WorkflowDatabaseError,
    WorkflowError,
    parse_review_command,
)

TENANT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
EMPLOYEE_ID = "22222222-2222-2222-2222-222222222222"
SUBMISSION_ID = "11111111-1111-1111-1111-111111111111"
REVIEW_REF = "YR-ABCD234EFG"
REQUEST_KEY = "phase4-test-key-001"
SENDER = "+2348000000001"


def _grant_body(action="granted", is_retry=False, request_key=REQUEST_KEY):
    return {"status": "ok", "action": action,
        "grant": {"tenant_id": TENANT_ID, "business_id": "b1",
            "branch_id": None, "employee_id": EMPLOYEE_ID,
            "can_confirm": True, "can_reject": True},
        "request_key": request_key, "is_retry": is_retry}


def _run(coro):
    return asyncio.run(coro)


def _admin_client(post_response):
    client = AsyncMock()
    client.post = AsyncMock(return_value=post_response)
    return client


def _run_admin(client, coro):
    with patch("supabase_backend.credentials",
               return_value=("https://example.supabase.co", "test-key")), \
         patch("review_service.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        return _run(coro), client


class AdminGrantTests(unittest.TestCase):
    def _grant(self, post_response=None, **kwargs):
        call = {"tenant_id": TENANT_ID, "business_id": "b1",
            "branch_id": None, "employee_id": EMPLOYEE_ID,
            "can_confirm": True, "can_reject": False,
            "request_key": REQUEST_KEY}
        call.update(kwargs)
        client = _admin_client(post_response or httpx.Response(200,
            json=_grant_body()))
        granted_body = dict(_grant_body())
        granted_body["grant"] = dict(granted_body["grant"],
            can_reject=False)
        if post_response is None:
            client.post = AsyncMock(return_value=httpx.Response(200,
                json=granted_body))
        return _run_admin(client, grant_reviewer(**call))

    def test_grant_calls_only_the_fixed_grant_rpc(self):
        result, client = self._grant()
        self.assertEqual(result["status"], "ok")
        client.post.assert_called_once()
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_grant_reviewer")

    def test_grant_payload_carries_exact_keys(self):
        _result, client = self._grant(reason="duty manager",
            actor="owner-console")
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(set(body),
            {"p_tenant_id", "p_business_id", "p_branch_id",
                "p_employee_id", "p_can_confirm", "p_can_reject",
                "p_reason", "p_actor", "p_request_key"})
        self.assertIsNone(body["p_branch_id"])
        self.assertEqual(body["p_reason"], "duty manager")

    def test_grant_cannot_reach_other_rpcs(self):
        client = _admin_client(httpx.Response(200, json=_grant_body()))
        with patch("supabase_backend.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("review_service.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(WorkflowError):
                _run(review_service._post_own_rpc(
                    "amose_revoke_reviewer",
                    review_service.GRANT_ALLOWLIST, {}))
            with self.assertRaises(WorkflowError):
                _run(review_service._post_own_rpc(
                    "amose_confirm_submission",
                    review_service.GRANT_ALLOWLIST, {}))
        client.post.assert_not_called()

    def test_capabilities_must_be_real_booleans(self):
        client = _admin_client(httpx.Response(200, json=_grant_body()))
        with patch("supabase_backend.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("review_service.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            for bad in (0, 1, "true", None):
                with self.assertRaises(ReviewValidationError, msg=str(bad)):
                    _run(grant_reviewer(TENANT_ID, "b1", None,
                        EMPLOYEE_ID, bad, True, REQUEST_KEY))
            with self.assertRaises(ReviewValidationError):
                _run(grant_reviewer(TENANT_ID, "b1", None,
                    EMPLOYEE_ID, False, False, REQUEST_KEY))
        client.post.assert_not_called()

    def test_bad_ids_rejected_before_network(self):
        client = _admin_client(httpx.Response(200, json=_grant_body()))
        with patch("supabase_backend.credentials",
                   return_value=("https://example.supabase.co", "test-key")), \
             patch("review_service.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(WorkflowError):
                _run(grant_reviewer("not-a-uuid", "b1", None,
                    EMPLOYEE_ID, True, True, REQUEST_KEY))
            with self.assertRaises(WorkflowError):
                _run(grant_reviewer(TENANT_ID, "b1", None,
                    EMPLOYEE_ID, True, True, ""))
        client.post.assert_not_called()

    def test_idempotent_grant_retry_accepted(self):
        body = _grant_body(is_retry=True)
        result, client = self._grant(
            post_response=httpx.Response(200, json=body))
        self.assertTrue(result["is_retry"])
        self.assertEqual(result["action"], "granted")

    def test_changed_key_reuse_maps_to_conflict(self):
        client = _admin_client(httpx.Response(400, json={
            "message": "CONFLICT: request key was already used for a different authorization act"}))
        with self.assertRaises(RequestConflictError):
            _run_admin(client, grant_reviewer(TENANT_ID, "b1", None,
                EMPLOYEE_ID, True, False, REQUEST_KEY))

    def test_unknown_scope_maps_to_not_found(self):
        client = _admin_client(httpx.Response(400, json={
            "message": "NOT_FOUND: business b9 does not exist in this tenant"}))
        with self.assertRaises(AuthorizationNotFoundError):
            _run_admin(client, grant_reviewer(TENANT_ID, "b9", None,
                EMPLOYEE_ID, True, True, REQUEST_KEY))

    def test_malformed_maps_to_admin_error(self):
        client = _admin_client(httpx.Response(400, json={
            "message": "MALFORMED: at least one capability is required"}))
        with self.assertRaises(AuthorizationAdminError):
            _run_admin(client, grant_reviewer(TENANT_ID, "b1", None,
                EMPLOYEE_ID, True, True, REQUEST_KEY))

    def test_malformed_grant_result_rejected(self):
        for body in (["ok"],
                dict(_grant_body(), action="superuser"),
                dict(_grant_body(), request_key="other"),
                {"status": "ok", "action": "granted",
                    "request_key": REQUEST_KEY, "is_retry": False}):
            client = _admin_client(httpx.Response(200, json=body))
            with self.assertRaises(WorkflowDatabaseError, msg=str(body)):
                _run_admin(client, grant_reviewer(TENANT_ID, "b1", None,
                    EMPLOYEE_ID, True, False, REQUEST_KEY))


class AdminRevokeTests(unittest.TestCase):
    def _revoke(self, post_response=None, **kwargs):
        call = {"tenant_id": TENANT_ID, "business_id": "b1",
            "branch_id": "br1", "employee_id": EMPLOYEE_ID,
            "request_key": REQUEST_KEY}
        call.update(kwargs)
        body = {"status": "ok", "action": "revoked",
            "grant": {"tenant_id": TENANT_ID, "business_id": "b1",
                "branch_id": "br1", "employee_id": EMPLOYEE_ID,
                "can_confirm": False, "can_reject": False},
            "request_key": REQUEST_KEY, "is_retry": False}
        client = _admin_client(post_response or httpx.Response(200,
            json=body))
        return _run_admin(client, revoke_reviewer(**call))

    def test_revoke_calls_only_the_fixed_revoke_rpc(self):
        result, client = self._revoke()
        self.assertEqual(result["action"], "revoked")
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_revoke_reviewer")
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(set(body),
            {"p_tenant_id", "p_business_id", "p_branch_id",
                "p_employee_id", "p_reason", "p_actor", "p_request_key"})

    def test_revoke_idempotent_already_revoked(self):
        body = {"status": "ok", "action": "already_revoked",
            "grant": {"tenant_id": TENANT_ID, "business_id": "b1",
                "branch_id": "br1", "employee_id": EMPLOYEE_ID,
                "can_confirm": False, "can_reject": False},
            "request_key": REQUEST_KEY, "is_retry": True}
        result, _client = self._revoke(
            post_response=httpx.Response(200, json=body))
        self.assertEqual(result["action"], "already_revoked")
        self.assertTrue(result["is_retry"])

    def test_revoke_conflict_maps_through(self):
        client = _admin_client(httpx.Response(400, json={
            "message": "CONFLICT: request key was already used for a different authorization act"}))
        with self.assertRaises(RequestConflictError):
            _run_admin(client, revoke_reviewer(TENANT_ID, "b1", "br1",
                EMPLOYEE_ID, REQUEST_KEY))


class AdminListTests(unittest.TestCase):
    def _list(self, post_response=None, **kwargs):
        call = {"tenant_id": TENANT_ID}
        call.update(kwargs)
        body = {"status": "ok", "tenant_id": TENANT_ID,
            "authorizations": [{"employee_id": EMPLOYEE_ID,
                "business_id": "b1", "branch_id": None,
                "can_confirm": True, "can_reject": True,
                "active": True, "updated_at": "2026-09-18T00:00:00+00:00"}]}
        client = _admin_client(post_response or httpx.Response(200,
            json=body))
        return _run_admin(client, list_reviewers(**call))

    def test_list_calls_only_the_fixed_list_rpc(self):
        result, client = self._list(business_id="b1")
        self.assertEqual(len(result["authorizations"]), 1)
        self.assertEqual(client.post.call_args.args[0],
            "/rest/v1/rpc/amose_list_reviewer_authorizations")
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(set(body),
            {"p_tenant_id", "p_business_id", "p_branch_id",
                "p_include_inactive"})

    def test_list_is_the_only_read_path_shape(self):
        """Entries expose scope + capabilities only: no sender identities,
        secrets, or keys -- the list RPC is the whole read surface."""
        result, _client = self._list()
        entry = result["authorizations"][0]
        self.assertEqual(set(entry),
            {"employee_id", "business_id", "branch_id", "can_confirm",
                "can_reject", "active", "updated_at"})

    def test_list_tenant_mismatch_rejected(self):
        body = {"status": "ok", "tenant_id": "other-tenant",
            "authorizations": []}
        client = _admin_client(httpx.Response(200, json=body))
        with self.assertRaises(WorkflowDatabaseError):
            _run_admin(client, list_reviewers(TENANT_ID))

    def test_list_unknown_tenant_maps_to_not_found(self):
        client = _admin_client(httpx.Response(400, json={
            "message": "NOT_FOUND: tenant does not exist"}))
        with self.assertRaises(AuthorizationNotFoundError):
            _run_admin(client, list_reviewers(TENANT_ID))


class AdminHierarchyTests(unittest.TestCase):
    def test_admin_errors_are_workflow_errors(self):
        self.assertTrue(issubclass(AuthorizationAdminError, WorkflowError))
        self.assertTrue(issubclass(AuthorizationNotFoundError,
            AuthorizationAdminError))
        self.assertTrue(issubclass(AuthorizationNotFoundError, WorkflowError))


def _queue_body(request_key="queuereq:inbox-1", status="queued"):
    return {"status": status, "review_ref": REVIEW_REF,
        "submission_id": SUBMISSION_ID, "submission_kind": "sale",
        "request_key": request_key, "notified": 2,
        "skipped": [], "is_retry": False}


def _confirm_body(request_key="k1", verified=None):
    return {"status": "confirmed", "review_action": "confirmed",
        "posting_type": "sale", "submission_kind": "sale",
        "submission_id": SUBMISSION_ID, "review_ref": REVIEW_REF,
        "request_key": request_key,
        "verified_snapshot": verified if verified is not None else {
            "kind": "sale", "lines": []},
        "audit_id": "audit-1", "is_retry": False,
        "sale_id": "sale-1", "brain_memory_id": "mem-1"}


def _reject_body(request_key="k1", reason="duplicate report"):
    return {"status": "rejected", "review_action": "rejected",
        "submission_kind": "sale", "submission_id": SUBMISSION_ID,
        "review_ref": REVIEW_REF, "request_key": request_key,
        "reason": reason, "audit_id": "audit-9", "is_retry": False}


def _batch_client(inbox_rows, identities, assignments=None, post=None,
        patch_status=204):
    """Mock batch client: GET dispatches on path, POST on RPC name."""
    client = AsyncMock()

    async def fake_get(path, params=None):
        if path == "/rest/v1/biz_message_inbox":
            return httpx.Response(200, json=inbox_rows)
        if path == "/rest/v1/biz_sender_identities":
            return httpx.Response(200, json=identities)
        if path == "/rest/v1/biz_assignments":
            return httpx.Response(200, json=assignments or [])
        if path == "/rest/v1/biz_submissions":
            return httpx.Response(200, json=[{"id": SUBMISSION_ID}])
        raise AssertionError("unexpected GET " + path)

    async def fake_post(path, json=None, params=None, headers=None):
        if post is not None:
            answer = post(path, json)
            if answer is not None:
                return answer
        if path == "/rest/v1/biz_submissions":
            return httpx.Response(201)
        if path == "/rest/v1/rpc/amose_queue_review_requests":
            return httpx.Response(200, json=_queue_body(
                request_key=json["p_request_key"]))
        raise AssertionError("unexpected POST " + path)

    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(side_effect=fake_post)
    client.patch = AsyncMock(return_value=httpx.Response(patch_status))
    return client


def _run_batch(client):
    with patch("message_processor.credentials",
               return_value=("https://example.supabase.co", "test-key")), \
         patch("message_processor.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        return _run(message_processor.process_inbox_batch()), client


def _command_row(body, row_id="inbox-cmd"):
    return {"id": row_id, "provider": "whatsapp",
        "provider_account": "acct-1",
        "payload": {"kind": "message", "event": {
            "from": SENDER, "type": "text", "text": {"body": body}}}}


def _review_identities():
    return [{"tenant_id": "t-1", "employee_id": "emp-reviewer"}]


class QueueTriggerTests(unittest.TestCase):
    def test_queue_payload_carries_no_scope(self):
        posted = {}

        def post(path, body):
            if isinstance(body, dict):
                posted.update(body)
            return None

        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Sold 50 bags at 500"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}],
            post=post)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["review_requests_queued"], 1)
        self.assertEqual(set(posted),
            {"p_submission_id", "p_request_key"})
        self.assertEqual(posted["p_request_key"], "queuereq:inbox-1")
        for forbidden in ("tenant_id", "business_id", "branch_id",
                "employee_id", "provider_account", "recipient"):
            self.assertNotIn(forbidden, posted)

    def test_queue_failure_never_breaks_ingestion(self):
        def post(path, body):
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                return httpx.Response(500, json={"message": "boom"})
            return None

        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Sold 50 bags at 500"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}],
            post=post)
        summary, client = _run_batch(client)
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["review_requests_failed"], 1)
        self.assertEqual(summary["review_requests_queued"], 0)
        patch_body = client.patch.call_args.kwargs["json"]
        self.assertEqual(patch_body, {"status": "processed"})

    def test_unreviewable_kind_skips_queue(self):
        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Good morning, please call me"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}])
        summary, client = _run_batch(client)
        self.assertEqual(summary["submitted"], 1)
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0].startswith("/rest/v1/rpc/")]
        self.assertEqual(rpc_posts, [])
        self.assertEqual(summary["review_requests_queued"], 0)

    def test_queue_unroutable_accepted_without_notification(self):
        def post(path, body):
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                answered = _queue_body(
                    request_key=body["p_request_key"],
                    status="reviewer_unroutable")
                answered["notified"] = 0
                return httpx.Response(200, json=answered)
            return None

        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Sold 50 bags at 500"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}],
            post=post)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["submitted"], 1)
        # Never reported as success: nobody was notified.
        self.assertEqual(summary["review_requests_queued"], 0)
        self.assertEqual(summary["review_requests_unroutable"], 1)
        self.assertEqual(summary["review_requests_failed"], 0)

    def test_queue_already_queued_maps_to_own_counter(self):
        def post(path, body):
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                answered = _queue_body(
                    request_key=body["p_request_key"],
                    status="already_queued")
                return httpx.Response(200, json=answered)
            return None

        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Sold 50 bags at 500"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}],
            post=post)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["review_requests_queued"], 0)
        self.assertEqual(summary["review_requests_already_queued"], 1)

    def test_queue_no_eligible_reviewer_maps_to_own_counter(self):
        def post(path, body):
            if path == "/rest/v1/rpc/amose_queue_review_requests":
                answered = _queue_body(
                    request_key=body["p_request_key"],
                    status="no_eligible_reviewer")
                answered["notified"] = 0
                return httpx.Response(200, json=answered)
            return None

        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Sold 50 bags at 500"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}],
            post=post)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["review_requests_queued"], 0)
        self.assertEqual(summary["review_requests_no_reviewer"], 1)
        self.assertEqual(summary["review_requests_failed"], 0)

    def test_queue_bad_arguments_rejected_before_network(self):
        client = _batch_client([], [])
        with self.assertRaises(WorkflowError):
            _run(queue_review_requests(client, "not-a-uuid", "k1"))
        with self.assertRaises(WorkflowError):
            _run(queue_review_requests(client, SUBMISSION_ID, ""))
        client.post.assert_not_called()

    def test_queue_malformed_result_rejected(self):
        async def post(path, json=None, params=None, headers=None):
            return httpx.Response(200, json=["queued"])

        client = _batch_client([], [])
        client.post = AsyncMock(side_effect=post)
        with self.assertRaises(WorkflowDatabaseError):
            _run(queue_review_requests(client, SUBMISSION_ID, "k1"))


def _confirm_post_fn(body=None, status=200):
    payload = body if body is not None else _confirm_body()

    def post(path, _body):
        if path == "/rest/v1/rpc/amose_review_confirm_command":
            return httpx.Response(status, json=payload)
        return None

    return post


class ReviewCommandWiringTests(unittest.TestCase):
    def _confirm_post(self, body=None, status=200):
        return _confirm_post_fn(body=body, status=status)

    def test_confirm_command_calls_exactly_one_rpc(self):
        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(),
            assignments=[],
            post=self._confirm_post())
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_confirmed"], 1)
        self.assertEqual(summary["submitted"], 0)
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0].startswith("/rest/v1/rpc/")]
        self.assertEqual(len(rpc_posts), 1)
        self.assertEqual(rpc_posts[0].args[0],
            "/rest/v1/rpc/amose_review_confirm_command")
        body = rpc_posts[0].kwargs["json"]
        self.assertEqual(set(body),
            {"p_review_ref", "p_reviewer_provider", "p_reviewer_sender",
                "p_request_key", "p_correction_reason", "p_corrections"})
        self.assertEqual(body["p_corrections"], {})
        self.assertEqual(body["p_reviewer_sender"], SENDER)
        self.assertEqual(client.patch.call_args.kwargs["json"],
            {"status": "processed"})
        # No submission read or write on the review path.
        get_paths = {c.args[0] for c in client.get.call_args_list}
        self.assertNotIn("/rest/v1/biz_submissions", get_paths)
        self.assertNotIn("/rest/v1/biz_assignments", get_paths)

    def test_reject_command_calls_exactly_one_rpc(self):
        def post(path, body):
            if path == "/rest/v1/rpc/amose_reject_submission":
                return httpx.Response(200, json=_reject_body(
                    request_key=body["p_request_key"],
                    reason=body["p_reason"]))
            return None

        client = _batch_client(
            [_command_row("REVIEW REJECT %s KEY k1 REASON duplicate"
                % REVIEW_REF)],
            _review_identities(), assignments=[], post=post)
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_rejected"], 1)
        self.assertEqual(summary["submitted"], 0)
        rpc_posts = [c for c in client.post.call_args_list
            if c.args[0].startswith("/rest/v1/rpc/")]
        self.assertEqual(len(rpc_posts), 1)
        self.assertEqual(rpc_posts[0].args[0],
            "/rest/v1/rpc/amose_reject_submission")

    def test_multi_assignment_reviewer_can_still_review(self):
        """Review commands bypass the single-assignment gate: reviewers
        routinely hold several branches, and scope comes from the
        reference inside the database, not from assignments."""
        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(),
            assignments=[{"business_id": "b-1", "branch_id": "br-1"},
                {"business_id": "b-1", "branch_id": "br-2"}],
            post=self._confirm_post())
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_confirmed"], 1)
        self.assertEqual(summary["unmatched"], 0)
        client.post.assert_called_once()

    def test_unauthorized_reviewer_refused_without_writes(self):
        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(400, json={
                    "message": "UNAUTHORIZED: reviewer could not be authorized"})
            return None

        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(), assignments=[], post=post)
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_confirmed"], 0)
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(summary["submitted"], 0)
        submission_posts = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(submission_posts, [])
        patch_body = client.patch.call_args.kwargs["json"]
        self.assertEqual(patch_body["status"], "processed")
        self.assertIn("processing_error", patch_body)

    def test_cross_tenant_guess_refused(self):
        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(400, json={
                    "message": "NOT_FOUND: review reference is not known"})
            return None

        client = _batch_client(
            [_command_row("REVIEW CONFIRM YR-WXYZ5678HJ KEY k1")],
            _review_identities(), assignments=[], post=post)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(summary["submitted"], 0)

    def test_casual_text_never_reviews(self):
        for body in ("yes", "ok", "confirm", "looks good",
                "CONFIRM %s" % REVIEW_REF):
            client = _batch_client([_command_row(body)],
                [{"tenant_id": "t-1", "employee_id": "emp-1"}],
                assignments=[{"business_id": "b-1", "branch_id": "br-1"}])
            summary, client = _run_batch(client)
            self.assertEqual(summary["submitted"], 1, msg=body)
            self.assertEqual(summary["reviews_confirmed"], 0, msg=body)
            self.assertEqual(summary["reviews_refused"], 0, msg=body)
            rpc_posts = [c for c in client.post.call_args_list
                if c.args[0].startswith("/rest/v1/rpc/")]
            # Only the post-draft review-request queue call may fire.
            self.assertTrue(all(
                c.args[0].endswith("amose_queue_review_requests")
                for c in rpc_posts), msg=body)

    def test_uuid_command_never_reviews(self):
        uuid_text = "REVIEW CONFIRM 11111111-1111-1111-1111-111111111111 KEY k1"
        client = _batch_client([_command_row(uuid_text)],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}])
        summary, client = _run_batch(client)
        # Reserved-prefix isolation: refused locally with zero submissions
        # and zero RPC calls -- never ingested as a report.
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["reviews_confirmed"], 0)
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(client.post.call_args_list, [])

    def test_malformed_reference_never_reviews(self):
        client = _batch_client(
            [_command_row("REVIEW CONFIRM YR-SHORT KEY k1")],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}])
        summary, client = _run_batch(client)
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(client.post.call_args_list, [])

    def test_missing_routing_refused(self):
        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(400, json={
                    "message": "ROUTING: original report sender has no verified sender identity"})
            return None

        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(), assignments=[], post=post)
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(summary["reviews_failed"], 0)
        self.assertEqual(client.patch.call_args.kwargs["json"]["status"],
            "processed")

    def test_transient_failure_marked_failed(self):
        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(500, json={"message": "down"})
            return None

        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(), assignments=[], post=post)
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_failed"], 1)
        self.assertEqual(summary["reviews_refused"], 0)
        self.assertEqual(client.patch.call_args.kwargs["json"]["status"],
            "failed")

    def test_same_command_retry_returns_original(self):
        payload = _confirm_body()
        payload["is_retry"] = True

        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(200, json=payload)
            return None

        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(), assignments=[], post=post)
        summary, client = _run_batch(client)
        self.assertEqual(summary["reviews_confirmed"], 1)
        client.post.assert_called_once()

    def test_changed_key_retry_refused(self):
        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(400, json={
                    "message": "CONFLICT: request key was already used with different verified data"})
            return None

        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY other-key" % REVIEW_REF)],
            _review_identities(), assignments=[], post=post)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["reviews_refused"], 1)
        self.assertEqual(summary["reviews_confirmed"], 0)

    def test_unknown_command_sender_unmatched_without_rpc(self):
        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            [], assignments=[])
        summary, client = _run_batch(client)
        self.assertEqual(summary["unmatched"], 1)
        client.post.assert_not_called()

    def test_command_rescan_finds_nothing(self):
        """Inbox idempotency: once the command row is processed, a fresh
        scan has nothing left to review."""
        client = _batch_client([], [], assignments=[])
        summary, client = _run_batch(client)
        self.assertEqual(summary["scanned"], 0)
        self.assertEqual(summary["reviews_confirmed"], 0)
        client.post.assert_not_called()
        client.patch.assert_not_called()


class NoDirectWritesTests(unittest.TestCase):
    def test_review_paths_touch_only_rpc_and_inbox(self):
        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            _review_identities(), assignments=[],
            post=_confirm_post_fn())
        _summary, client = _run_batch(client)
        post_paths = {c.args[0] for c in client.post.call_args_list}
        self.assertEqual(post_paths,
            {"/rest/v1/rpc/amose_review_confirm_command"})
        patch_paths = {c.args[0] for c in client.patch.call_args_list}
        self.assertEqual(patch_paths, {"/rest/v1/biz_message_inbox"})
        get_paths = {c.args[0] for c in client.get.call_args_list}
        self.assertEqual(get_paths,
            {"/rest/v1/biz_message_inbox", "/rest/v1/biz_sender_identities"})

    def test_ordinary_reports_still_draft_only(self):
        client = _batch_client(
            [{"id": "inbox-1", "provider": "whatsapp",
                "provider_account": "acct-1",
                "payload": {"kind": "message", "event": {
                    "from": "+12025550123", "type": "text",
                    "text": {"body": "Sold 50 bags at 500"}}}}],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}])
        summary, client = _run_batch(client)
        submission_posts = [c for c in client.post.call_args_list
            if c.args[0] == "/rest/v1/biz_submissions"]
        self.assertEqual(len(submission_posts), 1)
        self.assertEqual(
            submission_posts[0].kwargs["json"][0]["status"], "draft")
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["reviews_confirmed"], 0)
        self.assertEqual(summary["reviews_rejected"], 0)


class ConcurrentConfirmationTests(unittest.TestCase):
    def test_concurrent_confirms_serialize(self):
        """Two simultaneous chat confirms for one reference: the first wins,
        the loser gets the terminal refusal. Exactly two RPC attempts, one
        inbox row each, no shared-state corruption."""
        calls = []

        def post(path, _body):
            calls.append(path)
            if len(calls) == 1:
                return httpx.Response(200, json=_confirm_body())
            return httpx.Response(400, json={
                "message": "NOT_REVIEWABLE: submission x has status confirmed"})

        async def attempt(row_id):
            client = _batch_client(
                [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF,
                    row_id=row_id)],
                _review_identities(), assignments=[], post=post)
            with patch("message_processor.credentials",
                       return_value=("https://example.supabase.co",
                           "test-key")), \
                 patch("message_processor.httpx.AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                return await message_processor.process_inbox_batch()

        async def both():
            return await asyncio.gather(attempt("inbox-a"), attempt("inbox-b"))

        first, second = _run(both())
        confirmed = first["reviews_confirmed"] + second["reviews_confirmed"]
        refused = first["reviews_refused"] + second["reviews_refused"]
        self.assertEqual(confirmed, 1)
        self.assertEqual(refused, 1)
        self.assertEqual(len(calls), 2)


class DuplicateInboundTests(unittest.TestCase):
    def test_webhook_collapses_in_batch_duplicates(self):
        from whatsapp_webhook import extract_events

        def payload(event_id):
            return {"object": "whatsapp_business_account", "entry": [{
                "changes": [{"field": "messages", "value": {
                    "metadata": {"phone_number_id": "acct-1"},
                    "messages": [{"id": event_id, "from": SENDER,
                        "type": "text",
                        "text": {"body": "REVIEW CONFIRM %s KEY k1"
                            % REVIEW_REF}}]}}]}]}

        rows = extract_events(payload("wamid.1"))
        doubled = extract_events({"object": "whatsapp_business_account",
            "entry": payload("wamid.1")["entry"] + payload("wamid.1")["entry"]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(doubled), 1)
        self.assertEqual(doubled[0]["provider_event_id"],
            rows[0]["provider_event_id"])

    def test_duplicate_command_event_replays_idempotently(self):
        """The same command received as a second inbox row (new event id)
        with the same KEY returns the original result -- no second review."""
        payload = _confirm_body()
        payload["is_retry"] = True

        def post(path, _body):
            if path == "/rest/v1/rpc/amose_review_confirm_command":
                return httpx.Response(200, json=payload)
            return None

        for row_id in ("inbox-dup-1", "inbox-dup-2"):
            client = _batch_client(
                [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF,
                    row_id=row_id)],
                _review_identities(), assignments=[], post=post)
            summary, _client = _run_batch(client)
            self.assertEqual(summary["reviews_confirmed"], 1)


class ReservedPrefixTests(unittest.TestCase):
    def test_reserved_prefixes_claimed(self):
        for text in ("REVIEW CONFIRM YR-ABCD234EFG KEY k1",
                "REVIEW REJECT YR-ABCD234EFG KEY k1 REASON x",
                "REVIEW CONFIRM 11111111-1111-1111-1111-111111111111 KEY k1",
                "REVIEW REJECT YR-SHORT KEY k1 REASON x",
                "REVIEW CONFIRM YR-ABCD234EFG",
                "REVIEW CONFIRM",
                "REVIEW REJECT",
                "  review confirm YR-ABCD234EFG key k1  ",
                "review reject YR-ABCD234EFG key k1 reason x",
                "REVIEW CONFIRM YR-ABCD234EFG KEY k1 EXTRA WORDS"):
            self.assertTrue(review_service.is_reserved_command(text),
                msg=text)

    def test_unrelated_text_never_claimed(self):
        for text in ("yes", "ok", "confirm", "looks good",
                "Sold 50 bags at 500", "CONFIRM YR-ABCD234EFG",
                "REVIEW my report please", "REVIEW", "REVIEW FOO BAR",
                "", None, 123):
            self.assertFalse(review_service.is_reserved_command(text),
                msg=str(text))


class ReservedCommandIsolationTests(unittest.TestCase):
    """Malformed reserved-prefix texts are refused with zero submissions,
    zero extraction output, zero RPC calls, and zero operational/Brain
    writes. The refusal is local (no reference lookup), so it cannot
    reveal whether another tenant's reference exists."""

    def _refused_run(self, body):
        client = _batch_client([_command_row(body)],
            [{"tenant_id": "t-1", "employee_id": "emp-1"}],
            assignments=[{"business_id": "b-1", "branch_id": "br-1"}])
        return _run_batch(client)

    def test_malformed_reserved_commands_create_nothing(self):
        bodies = [
            "REVIEW CONFIRM 11111111-1111-1111-1111-111111111111 KEY k1",
            "REVIEW REJECT 11111111-1111-1111-1111-111111111111 KEY k1 REASON x",
            "REVIEW CONFIRM YR-SHORT KEY k1",
            "REVIEW CONFIRM YR-0000000000 KEY k1",
            "REVIEW CONFIRM %s" % REVIEW_REF,
            "REVIEW CONFIRM %s KEY " % REVIEW_REF,
            "REVIEW REJECT %s KEY k1" % REVIEW_REF,
            "REVIEW CONFIRM %s KEY k1 EXTRA WORDS" % REVIEW_REF,
            "REVIEW REJECT %s KEY k1 REASON " % REVIEW_REF,
            "REVIEW CONFIRM",
            "REVIEW REJECT",
        ]
        for body in bodies:
            summary, client = self._refused_run(body)
            self.assertEqual(summary["submitted"], 0, msg=body)
            self.assertEqual(summary["reviews_confirmed"], 0, msg=body)
            self.assertEqual(summary["reviews_rejected"], 0, msg=body)
            self.assertEqual(summary["reviews_refused"], 1, msg=body)
            # Zero writes of every kind: no submission insert, no RPC
            # (not even a lookup), only the inbox refusal note.
            self.assertEqual(client.post.call_args_list, [], msg=body)
            get_paths = {c.args[0] for c in client.get.call_args_list}
            self.assertEqual(get_paths,
                {"/rest/v1/biz_message_inbox",
                    "/rest/v1/biz_sender_identities"}, msg=body)
            patch_body = client.patch.call_args.kwargs["json"]
            self.assertEqual(patch_body["status"], "processed", msg=body)
            self.assertIn("RefusedReviewCommand",
                patch_body["processing_error"], msg=body)
            self.assertNotIn(REVIEW_REF.replace("YR-", ""),
                patch_body["processing_error"], msg=body)

    def test_unknown_sender_reserved_command_unmatched_without_writes(self):
        client = _batch_client(
            [_command_row("REVIEW CONFIRM %s KEY k1" % REVIEW_REF)],
            [], assignments=[])
        summary, client = _run_batch(client)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(summary["submitted"], 0)
        client.post.assert_not_called()

    def test_lowercase_valid_command_still_handled(self):
        client = _batch_client(
            [_command_row("review reject %s key k1 reason duplicate"
                % REVIEW_REF.lower())],
            _review_identities(), assignments=[],
            post=lambda path, body: httpx.Response(200, json=_reject_body(
                request_key=body["p_request_key"],
                reason=body["p_reason"]))
            if path == "/rest/v1/rpc/amose_reject_submission" else None)
        summary, _client = _run_batch(client)
        self.assertEqual(summary["reviews_rejected"], 1)
        self.assertEqual(summary["reviews_refused"], 0)


class ServiceHierarchyTests(unittest.TestCase):
    def test_service_error_is_workflow_error(self):
        from review_service import ReviewServiceError
        self.assertTrue(issubclass(ReviewServiceError, WorkflowError))


if __name__ == "__main__":
    unittest.main()
