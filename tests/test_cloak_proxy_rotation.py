# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import main as registration_main
from config import cloakbrowser, proxy, roxybrowser


class CloakProxyRotationTests(unittest.TestCase):
    def setUp(self):
        self.pool = ["http://route-a:1001", "http://route-b:1002", "http://route-c:1003"]
        proxy._PROXY_ROTATION_SIGNATURE = ()
        proxy._PROXY_ROTATION_QUEUE = []
        proxy._PROXY_ROTATION_INDEX = 0
        proxy._REGISTRATION_PROXY_LEASES.clear()

    def tearDown(self):
        proxy._REGISTRATION_PROXY_LEASES.clear()

    def test_recoverable_failures_rotate_until_success_without_reusing_route(self):
        attempts = []

        def register(**kwargs):
            attempts.append(kwargs["proxy"])
            if len(attempts) < 3:
                return {"success": False, "email": kwargs["email"], "error": "temporary page state"}
            return {"success": True, "email": kwargs["email"], "account_id": "fixture"}

        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "cloak"), patch.object(
            cloakbrowser, "CLOAK_PROXY_ROTATION_ATTEMPTS", 0
        ), patch.object(proxy, "get_proxy_pool", return_value=self.pool), patch(
            "core.registration_preflight.preflight_proxy", return_value={"ok": True, "country": "US"}
        ), patch("core.cloakbrowser_registration.run_cloak_registration", side_effect=register):
            result = registration_main.run_registration("fixture@example.test", "Fixture", "1990-01-01")

        self.assertTrue(result["success"])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 3)
        self.assertFalse(proxy._REGISTRATION_PROXY_LEASES)

    def test_concurrent_leases_are_distinct(self):
        with patch.object(proxy, "get_proxy_pool", return_value=self.pool):
            first = proxy.acquire_registration_proxy()
            second = proxy.acquire_registration_proxy()
            self.assertNotEqual(first, second)
            proxy.release_registration_proxy(first)
            proxy.release_registration_proxy(second)
        self.assertFalse(proxy._REGISTRATION_PROXY_LEASES)

    def test_duplicate_real_exit_ip_is_skipped_without_consuming_browser_attempt(self):
        browser_attempts = []
        preflights = iter([
            {"ok": True, "country": "US", "ip": "198.51.100.10"},
            {"ok": True, "country": "US", "ip": "198.51.100.10"},
            {"ok": True, "country": "US", "ip": "198.51.100.11"},
        ])

        def register(**kwargs):
            browser_attempts.append(kwargs["proxy"])
            if len(browser_attempts) == 1:
                return {"success": False, "email": kwargs["email"], "error": "temporary page state"}
            return {"success": True, "email": kwargs["email"], "account_id": "fixture"}

        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "cloak"), patch.object(
            cloakbrowser, "CLOAK_PROXY_ROTATION_ATTEMPTS", 0
        ), patch.object(proxy, "get_proxy_pool", return_value=self.pool), patch(
            "core.registration_preflight.preflight_proxy", side_effect=lambda *args, **kwargs: next(preflights)
        ), patch("core.cloakbrowser_registration.run_cloak_registration", side_effect=register):
            result = registration_main.run_registration("fixture@example.test", "Fixture", "1990-01-01")

        self.assertTrue(result["success"])
        self.assertEqual(len(browser_attempts), 2)
        self.assertEqual(len(set(browser_attempts)), 2)


if __name__ == "__main__":
    unittest.main()
