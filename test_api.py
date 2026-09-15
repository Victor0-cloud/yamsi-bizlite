import os
import unittest
from unittest.mock import patch
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
    def test_missing_inputs(self):
        with patch.dict(os.environ,{"YAMSI_API_KEY":"test-only-key"}):
            result=self.client.post("/calculate/poultry-profit",headers={"x-yamsi-key":"test-only-key"},json={})
            self.assertEqual(result.status_code,422)

if __name__=="__main__": unittest.main()

