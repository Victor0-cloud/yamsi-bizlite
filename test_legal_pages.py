"""Public legal pages for YAMSI Lite (Meta app-review compliance).

Verifies /privacy, /terms, and /data-deletion are unauthenticated,
return HTML 200, and carry the correct headings and contact details.
No test touches a database, makes a network call, or uses a real
credential -- page content is static.
"""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import legal_pages
from app import app

CONTACT = "victorotite6@gmail.com"
SUBJECT = "YAMSI Lite Data Deletion Request"

PAGES = {
    "/privacy": "YAMSI Lite Privacy Policy",
    "/terms": "YAMSI Lite Terms of Service",
    "/data-deletion": "YAMSI Lite Data Deletion",
}


class LegalPagesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_pages_are_public_html(self):
        # Public with or without an API key configured: no auth, no 401.
        for env in ({}, {"YAMSI_API_KEY": "test-only-owner-key"}):
            with patch.dict(os.environ, env, clear=True):
                for path in PAGES:
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200,
                        msg=path)
                    self.assertIn("text/html",
                        response.headers["content-type"], msg=path)

    def test_pages_carry_correct_headings(self):
        for path, heading in PAGES.items():
            response = self.client.get(path)
            self.assertIn("<h1>%s</h1>" % heading, response.text,
                msg=path)
            self.assertIn("<title>%s</title>" % heading, response.text,
                msg=path)

    def test_contact_email_on_every_page(self):
        for path in PAGES:
            response = self.client.get(path)
            self.assertIn(CONTACT, response.text, msg=path)

    def test_privacy_explains_collection_and_rights(self):
        body = self.client.get("/privacy").text.lower()
        for fragment in ("whatsapp", "retention", "sharing",
                "your rights", "/data-deletion"):
            self.assertIn(fragment, body, msg=fragment)

    def test_terms_covers_use_and_limits(self):
        body = self.client.get("/terms").text
        for fragment in ("Acceptable use", "Account responsibility",
                "Service availability", "Limitations"):
            self.assertIn(fragment, body, msg=fragment)

    def test_deletion_instructions_are_complete(self):
        body = self.client.get("/data-deletion").text
        self.assertIn(SUBJECT, body)
        for fragment in ("WhatsApp number", "business name",
                "verified before deletion"):
            self.assertIn(fragment, body, msg=fragment)

    def test_pages_are_mobile_friendly_and_deterministic(self):
        for path in PAGES:
            first = self.client.get(path).text
            self.assertIn('name="viewport"', first, msg=path)
            self.assertEqual(first, self.client.get(path).text,
                msg=path)

    def test_pages_expose_no_secrets(self):
        with patch.dict(os.environ,
                {"YAMSI_API_KEY": "test-only-owner-key",
                 "WHATSAPP_ACCESS_TOKEN": "test-only-access-token"},
                clear=True):
            for path in PAGES:
                body = self.client.get(path).text
                self.assertNotIn("test-only-owner-key", body, msg=path)
                self.assertNotIn("test-only-access-token", body,
                    msg=path)

    def test_module_constants_match_served_pages(self):
        self.assertIn(CONTACT, legal_pages.PRIVACY_HTML)
        self.assertIn(CONTACT, legal_pages.TERMS_HTML)
        self.assertIn(SUBJECT, legal_pages.DATA_DELETION_HTML)


if __name__ == "__main__":
    unittest.main()
