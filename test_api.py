import os
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app import app

class API(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
    def test_health(self):
        self.assertEqual(self.client.get("/health").status_code,200)
    def test_dashboard(self):
        response=self.client.get("/")
        self.assertEqual(response.status_code,200)
        self.assertIn("NOT CONNECTED", response.text)
    def test_unconfigured_is_locked(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(self.client.get("/business/list").status_code,503)
    def test_wrong_key(self):
        with patch.dict(os.environ,{"YAMSI_API_KEY":"test-only-key"}):
            self.assertEqual(self.client.get("/business/list").status_code,401)
    def test_authorized_businesses(self):
        with patch.dict(os.environ,{"YAMSI_API_KEY":"test-only-key"}), patch("app.read_businesses", return_value={"businesses":[{}, {}, {}]}):
            result=self.client.get("/business/list",headers={"x-yamsi-key":"test-only-key"})
            self.assertEqual(len(result.json()["businesses"]),3)
    def test_dispatch_outbound_requires_key(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}):
            result = self.client.post("/internal/dispatch-outbound", json={})
        self.assertEqual(result.status_code, 401)
    def test_dispatch_outbound_runs_bounded_pass(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}), \
             patch("outbound_dispatch_worker.dispatch_pending", new_callable=AsyncMock,
                   return_value={"scanned": 1, "sent": 1, "failed": 0, "claim_conflicts": 0}) as run:
            result = self.client.post("/internal/dispatch-outbound",
                headers={"x-yamsi-key": "test-only-key"}, json={"limit": 500})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["sent"], 1)
        run.assert_called_once_with(limit=100)
    def test_dispatch_outbound_default_limit(self):
        with patch.dict(os.environ, {"YAMSI_API_KEY": "test-only-key"}), \
             patch("outbound_dispatch_worker.dispatch_pending", new_callable=AsyncMock,
                   return_value={"scanned": 0, "sent": 0, "failed": 0, "claim_conflicts": 0}) as run:
            result = self.client.post("/internal/dispatch-outbound",
                headers={"x-yamsi-key": "test-only-key"}, json={})
        self.assertEqual(result.status_code, 200)
        run.assert_called_once_with(limit=20)
    def test_missing_inputs(self):
        with patch.dict(os.environ,{"YAMSI_API_KEY":"test-only-key"}):
            result=self.client.post("/calculate/poultry-profit",headers={"x-yamsi-key":"test-only-key"},json={})
            self.assertEqual(result.status_code,422)

if __name__=="__main__": unittest.main()

