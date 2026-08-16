# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import cloakbrowser as cloak_config
from config import email as email_config
from config import proxy as proxy_config
from config import roxybrowser as registration_config
from core.cloakbrowser_driver import build_cloak_driver
from core.resin_proxy_status import registration_proxy_status
from webui.app import create_app


class ResinRegistrationGateTests(unittest.TestCase):
    def test_status_requires_nonempty_pool_and_reachable_endpoint(self):
        with patch.object(proxy_config, "get_proxy_pool", return_value=[]), patch.object(
            proxy_config, "REGISTRATION_PROXY_REQUIRED", True, create=True
        ):
            status = registration_proxy_status(check_tcp=False)

        self.assertFalse(status["ready"])
        self.assertEqual(status["qualified_count"], 0)

    def test_cloak_driver_rejects_direct_connection_when_gate_enabled(self):
        with patch.object(cloak_config, "CLOAK_USE_PROXY", True), patch.object(
            proxy_config, "REGISTRATION_PROXY_REQUIRED", True, create=True
        ), patch.object(proxy_config, "pick_proxy", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "Resin"):
                build_cloak_driver(proxy=None)

    @patch("webui.app.svc.submit_registration")
    @patch("core.resin_proxy_status.registration_proxy_status")
    def test_jobs_rejects_when_resin_gate_is_not_ready(self, proxy_status, submit_registration):
        proxy_status.return_value = {
            "required": True,
            "ready": False,
            "error": "合格代理池为空",
            "qualified_count": 0,
        }
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        with patch.object(registration_config, "REGISTRATION_DRIVER", "cloak"), patch.object(
            proxy_config, "REGISTRATION_PROXY_REQUIRED", True, create=True
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "configured"):
            response = client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 503)
        self.assertIn("Resin", response.get_json()["error"])
        submit_registration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
