# -*- coding: utf-8 -*-
"""补密码协议版 OTP 回归：必须显式触发发信；首轮超时重发一次（2026-10-17 修复）。

背景：add-password 三次 OTP 超时、邮箱零新邮件。2FA/查活链路早已证明
"仅跟随 authorize 有时只打开页面不真正投递邮件"，必须显式 send_email_otp。
"""
import unittest
from unittest import mock

import core.account_password as account_password


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, **kwargs):
        self.device_id = "dev-123"
        self.signin_calls = 0

    def get_chatgpt_headers(self, referer=None, **kw):
        return {}

    def get_nextauth_headers(self, referer=None, **kw):
        return {}

    def get_auth_headers(self, referer=None, **kw):
        return {}

    def get_auth_navigate_headers(self, referer=None, **kw):
        return {}

    def get(self, url, headers=None, **kw):
        if "eligibility" in url:
            return _Resp(200, {})
        if "api/auth/csrf" in url:
            return _Resp(200, {"csrfToken": "csrf"})
        return _Resp(200, {})

    def post(self, url, headers=None, data=None, **kw):
        if "signin/openai" in url:
            self.signin_calls += 1
            return _Resp(200, {"url": f"https://auth.example/authorize?round={self.signin_calls}"})
        if "password/add" in url:
            return _Resp(200, {})
        return _Resp(200, {})


class ProtocolAddPasswordOtpTests(unittest.TestCase):
    def _run(self, follow_returns, wait_side_effect):
        session = _FakeSession()
        send_mock = mock.Mock()
        follow_mock = mock.Mock(return_value=follow_returns)
        with mock.patch("core.db.get_account_by_email",
                        return_value={"id": 1, "access_token": "AT-1", "password": ""}), \
             mock.patch("core.db.mark_account_password_verified") as verified, \
             mock.patch.object(account_password, "_resolve_fresh_account_route",
                               return_value=("http://proxy.local:1", {"proxy": "p"})), \
             mock.patch("core.session.BrowserSession", side_effect=lambda **kw: session), \
             mock.patch("core.session.close_browser_session"), \
             mock.patch("core.cloakbrowser_driver.account_fingerprint_seed", return_value="seed"), \
             mock.patch("core.account_export._follow_reauth_with_retry", follow_mock), \
             mock.patch("core.account_export._validate_reauth_otp",
                        return_value="https://auth.openai.com/reset-password/new-password"), \
             mock.patch("core.openai_auth.send_email_otp", send_mock), \
             mock.patch("core.openai_auth.request_sentinel_token", return_value=_Resp(200, {})), \
             mock.patch("core.openai_auth.build_sentinel_header", return_value=("sentinel", None)), \
             mock.patch("core.email_provider.wait_for_otp", side_effect=wait_side_effect), \
             mock.patch("core.roxy_registration._registration_password", return_value="Pw-123"):
            result = account_password.add_password_protocol("otp-a@example.com", save=False)
        return result, session, send_mock, follow_mock, verified

    def test_explicit_send_then_resend_then_success(self):
        waits = []

        def wait(email, after_ts=None, max_wait=None, used_codes=None, otp_state=None, **kw):
            waits.append({"after": after_ts, "max": max_wait})
            if len(waits) == 1:
                raise TimeoutError("iCloud OTP wait timed out")
            return "123456"

        result, session, send_mock, follow_mock, verified = self._run(
            "https://auth.openai.com/email-verification", wait)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("password"), "Pw-123")
        # 首轮显式发信 + 超时后重发一次
        self.assertEqual(send_mock.call_count, 2)
        self.assertEqual(session.signin_calls, 2)
        self.assertEqual(follow_mock.call_count, 2)
        self.assertEqual(len(waits), 2)
        # 第二轮等待的时间下界被放宽（after_ts 提前），避免第一封迟到被过滤
        self.assertLessEqual(waits[1]["after"], waits[0]["after"])
        verified.assert_called_once_with("otp-a@example.com")

    def test_both_waits_timeout_fails_with_two_sends(self):
        def always_timeout(email, after_ts=None, max_wait=None, used_codes=None, otp_state=None, **kw):
            raise TimeoutError("iCloud OTP wait timed out")

        result, session, send_mock, follow_mock, verified = self._run(
            "https://auth.openai.com/email-verification", always_timeout)
        self.assertFalse(result.get("ok"))
        self.assertIn("timed out", str(result.get("error") or ""))
        self.assertEqual(send_mock.call_count, 2)
        self.assertEqual(session.signin_calls, 2)
        verified.assert_not_called()

    def test_no_otp_page_skips_send(self):
        def should_not_be_called(*a, **kw):
            raise AssertionError("非 OTP 落点不应等待邮箱")

        result, session, send_mock, follow_mock, verified = self._run(
            "https://chatgpt.com/", should_not_be_called)
        self.assertTrue(result.get("ok"), result)
        send_mock.assert_not_called()
        self.assertEqual(session.signin_calls, 1)


if __name__ == "__main__":
    unittest.main()
