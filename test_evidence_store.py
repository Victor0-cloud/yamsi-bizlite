import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import evidence_store

TOKEN = "super-secret-media-test-token"

IMAGE_EVENT = {
    "id": "wamid.ABC123",
    "from": "+12025550123",
    "timestamp": "1758000000",
    "type": "image",
    "image": {"id": "media-xyz", "mime_type": "image/jpeg", "caption": "crates"},
}


class ExtractImageMetadataTests(unittest.TestCase):
    # 10. image media ID/caption extraction
    def test_extracts_all_fields(self):
        metadata = evidence_store.extract_image_metadata(IMAGE_EVENT)
        self.assertEqual(metadata["media_id"], "media-xyz")
        self.assertEqual(metadata["mime_type"], "image/jpeg")
        self.assertEqual(metadata["caption"], "crates")
        self.assertEqual(metadata["whatsapp_message_id"], "wamid.ABC123")
        self.assertEqual(metadata["sender"], "+12025550123")
        self.assertEqual(metadata["timestamp"], "1758000000")

    def test_non_image_event_returns_none(self):
        self.assertIsNone(evidence_store.extract_image_metadata({"type": "text"}))

    def test_image_without_caption(self):
        event = {"id": "m1", "from": "x", "timestamp": "1", "type": "image", "image": {"id": "media-1"}}
        metadata = evidence_store.extract_image_metadata(event)
        self.assertIsNone(metadata["caption"])


class CreateRequirementTests(unittest.TestCase):
    # 3. crate report without image: evidence required
    def test_creates_required_row(self):
        with patch("evidence_store.rest_get", new_callable=AsyncMock,
                    side_effect=[[], [{"id": "ev-1", "status": "required"}]]) as rget, \
             patch("evidence_store.rest_post", new_callable=AsyncMock) as rpost:
            result = asyncio.run(evidence_store.create_requirement("tenant-1", "nughe_farms", "warri", "sub-1"))
        self.assertEqual(result["status"], "required")
        rpost.assert_called_once()
        posted = rpost.call_args.args[1][0]
        self.assertEqual(posted["status"], "required")
        self.assertEqual(posted["subject_type"], "submission")

    # 9. idempotent: an existing requirement for the same submission is reused
    def test_idempotent_no_duplicate(self):
        with patch("evidence_store.rest_get", new_callable=AsyncMock,
                    return_value=[{"id": "ev-existing", "status": "required"}]) as rget, \
             patch("evidence_store.rest_post", new_callable=AsyncMock) as rpost:
            result = asyncio.run(evidence_store.create_requirement("tenant-1", "nughe_farms", "warri", "sub-1"))
        self.assertEqual(result["id"], "ev-existing")
        rpost.assert_not_called()


class HandleIncomingImageTests(unittest.TestCase):
    # 4. crate report with linked image: evidence received
    # 11. image linked to correct report/incident
    def test_links_single_open_requirement(self):
        with patch("evidence_store.rest_get", new_callable=AsyncMock,
                    return_value=[{"id": "ev-1", "submission_id": "sub-1"}]) as rget, \
             patch("evidence_store.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(evidence_store.handle_incoming_image(
                "tenant-1", "nughe_farms", "warri", "inbox-1", IMAGE_EVENT, employee_id="amos-emp"))
        self.assertEqual(result["linked_evidence_id"], "ev-1")
        self.assertEqual(result["ambiguous_candidate_ids"], [])
        get_params = rget.call_args.args[1]
        self.assertEqual(get_params["employee_id"], "eq.amos-emp")
        self.assertEqual(get_params["submission_business_id"], "eq.nughe_farms")
        self.assertEqual(get_params["submission_branch_id"], "eq.warri")
        patch_params = rpatch.call_args.args[1]
        self.assertEqual(patch_params["id"], "eq.ev-1")
        self.assertEqual(patch_params["provider_media_id"], "is.null")
        body = rpatch.call_args.args[2]
        self.assertEqual(body["status"], "received")
        self.assertEqual(body["provider_media_id"], "media-xyz")
        self.assertEqual(body["inbox_id"], "inbox-1")

    def test_no_open_requirement_returns_none_but_does_not_error(self):
        with patch("evidence_store.rest_get", new_callable=AsyncMock, return_value=[]), \
             patch("evidence_store.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(evidence_store.handle_incoming_image(
                "tenant-1", "nughe_farms", "warri", "inbox-1", IMAGE_EVENT, employee_id="amos-emp"))
        self.assertIsNone(result["linked_evidence_id"])
        rpatch.assert_not_called()

    def test_no_employee_id_returns_unlinked(self):
        result = asyncio.run(evidence_store.handle_incoming_image(
            "tenant-1", "nughe_farms", "warri", "inbox-1", IMAGE_EVENT, employee_id=None))
        self.assertIsNone(result["linked_evidence_id"])
        self.assertEqual(result["ambiguous_candidate_ids"], [])

    # 13. ambiguous image linkage does not guess
    def test_multiple_candidates_no_caption_marks_ambiguous(self):
        candidates = [{"id": "ev-1", "submission_id": "sub-1"}, {"id": "ev-2", "submission_id": "sub-2"}]
        event = dict(IMAGE_EVENT)
        event["image"] = dict(event["image"])
        event["image"]["caption"] = None
        with patch("evidence_store.rest_get", new_callable=AsyncMock, return_value=candidates) as rget, \
             patch("evidence_store.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(evidence_store.handle_incoming_image(
                "tenant-1", "nughe_farms", "warri", "inbox-1", event, employee_id="amos-emp"))
        self.assertIsNone(result["linked_evidence_id"])
        self.assertEqual(set(result["ambiguous_candidate_ids"]), {"ev-1", "ev-2"})
        rpatch.assert_not_called()

    # 12. image links to one unambiguous pending evidence requirement (caption resolves it)
    def test_multiple_candidates_caption_disambiguates(self):
        candidates = [{"id": "ev-crate", "submission_id": "sub-crate"}, {"id": "ev-mortality", "submission_id": "sub-mortality"}]
        submissions = {
            "sub-crate": [{"payload": {"parsed": {"fields": {"crates": 26}}}}],
            "sub-mortality": [{"payload": {"parsed": {"fields": {"mortality_count": 3}}}}],
        }
        event = dict(IMAGE_EVENT)
        event["image"] = dict(event["image"])
        event["image"]["caption"] = "the dead bird"

        async def fake_get(path, params=None):
            if path == "/rest/v1/biz_evidence":
                return candidates
            if path == "/rest/v1/biz_submissions":
                submission_id = params["id"].split(".", 1)[1]
                return submissions[submission_id]
            raise AssertionError("unexpected GET " + path)
        with patch("evidence_store.rest_get", new_callable=AsyncMock, side_effect=fake_get), \
             patch("evidence_store.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(evidence_store.handle_incoming_image(
                "tenant-1", "nughe_farms", "warri", "inbox-1", event, employee_id="amos-emp"))
        self.assertEqual(result["linked_evidence_id"], "ev-mortality")
        self.assertEqual(result["ambiguous_candidate_ids"], [])
        rpatch.assert_called_once()

    def test_ambiguous_caption_still_refuses_to_guess(self):
        # caption mentions both crate and mortality hints -- no clear signal
        candidates = [{"id": "ev-1", "submission_id": "sub-1"}, {"id": "ev-2", "submission_id": "sub-2"}]
        event = dict(IMAGE_EVENT)
        event["image"] = dict(event["image"])
        event["image"]["caption"] = "crates and dead birds"
        with patch("evidence_store.rest_get", new_callable=AsyncMock, return_value=candidates), \
             patch("evidence_store.rest_patch", new_callable=AsyncMock) as rpatch:
            result = asyncio.run(evidence_store.handle_incoming_image(
                "tenant-1", "nughe_farms", "warri", "inbox-1", event, employee_id="amos-emp"))
        self.assertIsNone(result["linked_evidence_id"])
        self.assertEqual(len(result["ambiguous_candidate_ids"]), 2)
        rpatch.assert_not_called()


class BuildStoragePathTests(unittest.TestCase):
    # 11. private evidence object path generated correctly
    def test_path_is_deterministic_and_isolated(self):
        path = evidence_store.build_storage_path("tenant-1", "nughe_farms", "warri", "ev-1", "media-1", "image/jpeg")
        self.assertEqual(path, "tenant-1/nughe_farms/warri/ev-1/media-1.jpg")

    def test_unknown_mime_type_falls_back_safely(self):
        path = evidence_store.build_storage_path("tenant-1", "nughe_farms", "warri", "ev-1", "media-1", None)
        self.assertTrue(path.endswith(".bin"))

    # 12. no public evidence URL is ever produced -- the path is a bucket-relative
    # object key, never an http(s) URL
    def test_path_is_never_a_url(self):
        path = evidence_store.build_storage_path("tenant-1", "nughe_farms", "warri", "ev-1", "media-1", "image/png")
        self.assertFalse(path.startswith("http"))
        self.assertNotIn("supabase.co", path)


class FetchAndStoreMediaTests(unittest.TestCase):
    def _mock_client_sequence(self, responses):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=responses)
        return client

    # 9 & 10. media metadata retrieval mocked, media binary retrieval mocked
    def test_successful_fetch_uploads_to_private_storage(self):
        meta_response = httpx.Response(200, json={"url": "https://mock-meta-cdn/file", "mime_type": "image/jpeg"})
        binary_response = httpx.Response(200, content=b"fake-jpeg-bytes")
        meta_client = self._mock_client_sequence([meta_response, binary_response])
        storage_client = AsyncMock()
        storage_client.post = AsyncMock(return_value=httpx.Response(200))
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.credentials", return_value=("https://example.supabase.co", "service-role-key")), \
             patch("evidence_store.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.side_effect = [meta_client, storage_client]
            result = asyncio.run(evidence_store.fetch_and_store_media("media-1", "tenant-1/nughe_farms/warri/ev-1/media-1.jpg"))
        self.assertEqual(result, "tenant-1/nughe_farms/warri/ev-1/media-1.jpg")
        upload_path = storage_client.post.call_args.args[0]
        self.assertIn(evidence_store.EVIDENCE_BUCKET, upload_path)
        self.assertNotIn(TOKEN, upload_path)

    def test_metadata_failure_raises_media_retrieval_error(self):
        meta_client = self._mock_client_sequence([httpx.Response(404)])
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = meta_client
            with self.assertRaises(evidence_store.MediaRetrievalError):
                asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg"))

    def test_binary_download_failure_raises_media_retrieval_error(self):
        meta_response = httpx.Response(200, json={"url": "https://mock-meta-cdn/file", "mime_type": "image/jpeg"})
        meta_client = self._mock_client_sequence([meta_response, httpx.Response(403)])
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = meta_client
            with self.assertRaises(evidence_store.MediaRetrievalError):
                asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg"))

    # 14. missing WHATSAPP_ACCESS_TOKEN fails safely
    def test_missing_token_fails_safely(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(evidence_store.MediaRetrievalError):
                asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg"))

    # 15. secret never appears in logs/errors
    def test_token_never_appears_in_errors(self):
        meta_client = self._mock_client_sequence([httpx.Response(500)])
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = meta_client
            try:
                asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg"))
            except evidence_store.MediaRetrievalError as error:
                self.assertNotIn(TOKEN, str(error))

    # failed media retrieval/storage receives retry state
    def test_failure_records_retry_state_when_evidence_context_given(self):
        meta_client = self._mock_client_sequence([httpx.Response(500)])
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.httpx.AsyncClient") as factory, \
             patch("evidence_store.retry_engine.record_failure", new_callable=AsyncMock) as record_failure:
            factory.return_value.__aenter__.return_value = meta_client
            with self.assertRaises(evidence_store.MediaRetrievalError):
                asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg",
                    evidence_id="ev-1", tenant_id="tenant-1", business_id="nughe_farms", branch_id="warri"))
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[1], "media_retrieval")
        self.assertEqual(record_failure.call_args.args[2], "ev-1")

    def test_success_records_retry_resolution_when_evidence_context_given(self):
        meta_response = httpx.Response(200, json={"url": "https://mock-meta-cdn/file", "mime_type": "image/jpeg"})
        binary_response = httpx.Response(200, content=b"fake-jpeg-bytes")
        meta_client = self._mock_client_sequence([meta_response, binary_response])
        storage_client = AsyncMock()
        storage_client.post = AsyncMock(return_value=httpx.Response(200))
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.credentials", return_value=("https://example.supabase.co", "service-role-key")), \
             patch("evidence_store.httpx.AsyncClient") as factory, \
             patch("evidence_store.retry_engine.record_success", new_callable=AsyncMock) as record_success:
            factory.return_value.__aenter__.side_effect = [meta_client, storage_client]
            asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg",
                evidence_id="ev-1", tenant_id="tenant-1"))
        record_success.assert_called_once_with("tenant-1", "media_retrieval", "ev-1")

    def test_no_evidence_context_skips_retry_tracking(self):
        meta_client = self._mock_client_sequence([httpx.Response(500)])
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("evidence_store.httpx.AsyncClient") as factory, \
             patch("evidence_store.retry_engine.record_failure", new_callable=AsyncMock) as record_failure:
            factory.return_value.__aenter__.return_value = meta_client
            with self.assertRaises(evidence_store.MediaRetrievalError):
                asyncio.run(evidence_store.fetch_and_store_media("media-1", "some/path.jpg"))
        record_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
