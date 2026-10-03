# -*- coding: utf-8 -*-
"""回归：authorize 落到 email-verification（邮箱验证墙）时，协议登录自动转邮箱 OTP（含 MFA）。

背景：2026-09-24 批部分账号（"组合流程补设 2FA"）下一次登录会被强制邮箱验证：
authorize 直接落到 /email-verification，password/verify 必然 409 invalid_state。
修复：识别该落地页 → 等邮箱验证码（页面自动发码，超时重发一次）→ validate →
如有 MFA 提交 TOTP → 跟随 continue_url 取 OAuth code → 正常换 token。
"""
import unittest
from unittest.mock import patch

import core.password_login as password_login


class _Resp:
    def __init__(self, url, status_code=200, text=""):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = {}


class _DummySession:
    def __init__(self, *a, **kw):
        self.device_id = "device-test"
        self.proxy = None

    def get_auth_navigate_headers(self, referer=""):
        return {}

    def get(self, url, headers=None, allow_redirects=False, **kw):
        if "/api/accounts/authorize" in str(url):
            return _Resp("https://auth.openai.com/email-verification")
        return _Resp("https://auth.openai.com/")

    def post(self, *a, **kw):
        raise AssertionError("邮箱验证墙分支不应提交密码")

    def reset_circuit_breaker(self):
        pass

    def close(self):
        pass


class EmailVerificationLoginTests(unittest.TestCase):
    def test_login_switches_to_email_otp_when_wall(self):
        calls = {}

        def _fake_finish(session, email, timeout=30):
            calls["email"] = email
            return "auth-code-123"

        with patch("core.session.BrowserSession", _DummySession), \
             patch.object(password_login, "_finish_email_verification_login", side_effect=_fake_finish), \
             patch("core.codex_oauth.exchange_codex_token",
                   return_value={"access_token": "at-1", "refresh_token": "rt-1", "id_token": "id-1"}) as ex:
            res = password_login.login_with_password("user@example.com", "pw")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(calls.get("email"), "user@example.com")
        self.assertEqual(res.get("access_token"), "at-1")
        steps = [s.get("name") for s in res.get("steps") or []]
        self.assertIn("email_verification", steps)
        ex.assert_called_once()

    def test_helper_mfa_branch_returns_code(self):
        class _WaitSession:
            def __init__(self, *a, **kw):
                pass

            def wait(self, email, after_ts, **kw):
                return "123456"

            def mark_used(self, code):
                pass

        with patch("core.email_provider.OtpWaitSession", _WaitSession), \
             patch("core.db.get_account_by_email", return_value={}), \
             patch("core.openai_auth.validate_email_otp",
                   return_value={"page": {"type": "mfa_challenge"},
                                 "continue_url": "https://auth.openai.com/mfa-challenge/xyz"}), \
             patch("core.account_liveness._pass_mfa_challenge",
                   return_value={"continue_url": "https://platform.openai.com/auth/callback?code=abc999"}):
            code = password_login._finish_email_verification_login(object(), "user@example.com")
        self.assertEqual(code, "abc999")

    def test_helper_direct_code_without_mfa(self):
        class _WaitSession:
            def __init__(self, *a, **kw):
                pass

            def wait(self, email, after_ts, **kw):
                return "222333"

            def mark_used(self, code):
                pass

        with patch("core.email_provider.OtpWaitSession", _WaitSession), \
             patch("core.db.get_account_by_email", return_value={}), \
             patch("core.openai_auth.validate_email_otp",
                   return_value={"continue_url": "https://platform.openai.com/auth/callback?code=direct1"}):
            code = password_login._finish_email_verification_login(object(), "user@example.com")
        self.assertEqual(code, "direct1")


if __name__ == "__main__":
    unittest.main()
