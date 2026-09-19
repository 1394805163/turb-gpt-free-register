# -*- coding: utf-8 -*-
"""账密+2FA 协议登录必须携带账号级稳定指纹种子（防"每次登录都是新设备"）。"""
import unittest
from unittest.mock import patch

import core.password_login as password_login


class _DummySession:
    created = []

    def __init__(self, proxy=None, fingerprint_seed=None, **kwargs):
        self.proxy = proxy
        self.fingerprint_seed = fingerprint_seed
        self.device_id = "device-test"
        _DummySession.created.append(self)

    def get_auth_navigate_headers(self, referer=""):
        return {}

    def get(self, *args, **kwargs):
        raise RuntimeError("stop-early-for-test")

    def reset_circuit_breaker(self):
        pass

    def close(self):
        pass


class PasswordLoginFingerprintTests(unittest.TestCase):
    def setUp(self):
        _DummySession.created = []

    def test_session_pins_account_level_fingerprint_seed(self):
        with patch("core.session.BrowserSession", _DummySession), \
             patch("core.cloakbrowser_driver.account_fingerprint_seed", return_value="seed-for-user") as mocked, \
             patch.object(password_login.time, "sleep"):
            res = password_login.login_with_password("user@example.com", "pw")
        self.assertFalse(res.get("ok"))
        self.assertTrue(_DummySession.created, "BrowserSession 未被创建")
        self.assertEqual(_DummySession.created[0].fingerprint_seed, "seed-for-user")
        mocked.assert_called_once_with("user@example.com")

    def test_explicit_fingerprint_seed_wins(self):
        with patch("core.session.BrowserSession", _DummySession), \
             patch.object(password_login.time, "sleep"):
            password_login.login_with_password("user@example.com", "pw", fingerprint_seed="explicit-seed")
        self.assertEqual(_DummySession.created[0].fingerprint_seed, "explicit-seed")


if __name__ == "__main__":
    unittest.main()
