# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class ICloudResinFrontendTests(unittest.TestCase):
    def test_modern_ui_exposes_icloud_import_and_resin_status(self):
        template = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-value="icloud"', template)
        self.assertIn('id="resinRegistrationStatusV2"', template)
        self.assertIn('id="btnTestResinProxyV2"', template)
        self.assertIn("/api/registration/proxy-status", template)
        self.assertIn("/api/registration/proxy-test", template)

    @patch("core.resin_proxy_status.registration_proxy_status")
    def test_proxy_status_api_does_not_expose_credentials(self, status):
        status.return_value = {
            "required": True,
            "ready": True,
            "qualified_count": 9,
            "endpoint": "http://127.0.0.1:2260",
            "online": True,
            "error": "",
        }
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        response = client.get("/api/registration/proxy-status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["qualified_count"], 9)
        self.assertNotIn("password", str(body).lower())
        self.assertNotIn("token", str(body).lower())


if __name__ == "__main__":
    unittest.main()
