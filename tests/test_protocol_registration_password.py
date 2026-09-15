# -*- coding: utf-8 -*-
"""协议注册：密码注册分支（create_account_password）的回归测试。

服务端给新邮箱返回 create_account_password 时必须先提交密码再走 OTP，
否则会把"可以用密码注册的新邮箱"误判成 not_fresh 直接丢掉。
"""
import unittest
from unittest.mock import Mock, patch


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if isinstance(payload, dict) else {}
        self.text = text or "{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class ProtocolRegistrationPasswordTests(unittest.TestCase):
    def _run(self, *, page_type: str, password_page_type: str = "email_otp_send"):
        from core import protocol_registration as pr

        calls: list[tuple[str, dict]] = []

        def fake_post(session, url, payload, referer=None, **kwargs):
            calls.append((url, payload))
            if url.endswith("/authorize/continue"):
                return _Resp(200, {"page": {"type": page_type}})
            if url.endswith("/user/register"):
                return _Resp(200, {"page": {"type": password_page_type}})
            if url.endswith("/email-otp/validate"):
                return _Resp(200, {"page": {"type": "email_otp_verification"}, "continue_url": "https://chatgpt.com/"})
            raise AssertionError(f"unexpected POST {url}")

        driver = Mock()
        # 默认落点还是邮箱输入页（没有 code 输入框）→ 流程会照常提交邮箱
        driver.execute_script.return_value = {"url": "https://auth.openai.com/log-in", "inputs": [{"type": "email", "name": "email"}]}
        session = Mock()
        saved: dict = {}

        def fake_save(**kwargs):
            saved.update(kwargs)
            return 42

        with patch.object(pr, "time") as fake_time, patch(
            "core.cloakbrowser_driver.build_cloak_driver", return_value=(driver, Mock())
        ), patch("core.page_session.PageSession", return_value=session), patch(
            "core.chatgpt_auth.signin_openai", return_value="https://auth.openai.com/api/accounts/authorize?x=1"
        ), patch("core.codex_oauth._post_json", side_effect=fake_post), patch(
            "core.email_provider.wait_for_otp", return_value="123456"
        ), patch(
            "core.account_export.fetch_session", return_value={"accessToken": "AT-1", "user": {}, "account": {}}
        ), patch(
            "core.account_export.save_account_data", side_effect=fake_save
        ):
            fake_time.sleep = lambda *_: None
            fake_time.time = lambda: 0.0
            result = pr.run_protocol_registration("fresh@icloud.com")

        return result, calls, saved, session

    def test_password_branch_creates_password_then_verifies_email(self):
        result, calls, saved, session = self._run(page_type="create_account_password")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "registered")
        self.assertTrue(result["registration_password"])
        urls = [url for url, _ in calls]
        self.assertIn("https://auth.openai.com/api/accounts/user/register", urls)
        self.assertLess(urls.index("https://auth.openai.com/api/accounts/user/register"),
                        urls.index("https://auth.openai.com/api/accounts/email-otp/validate"))
        register_body = dict(calls[urls.index("https://auth.openai.com/api/accounts/user/register")][1])
        self.assertEqual(register_body["username"], "fresh@icloud.com")
        self.assertEqual(register_body["password"], result["registration_password"])
        # 密码页要验证码时，必须先触发发送
        self.assertTrue(any("email-otp/send" in str(c.args[0]) for c in session.get.call_args_list))
        # 密码落盘到 extra
        self.assertEqual(saved["extra"]["registration_password"], result["registration_password"])
        self.assertEqual(saved["totp_secret"], None)

    def test_already_on_code_page_skips_email_submit(self):
        """login_hint 让服务端先走到验证码页时，重复提交邮箱会把新邮箱误判成已注册。"""
        from core import protocol_registration as pr
        from unittest.mock import Mock as _Mock, patch as _patch

        calls = []

        def fake_post(session, url, payload, referer=None, **kwargs):
            calls.append(url)
            if url.endswith("/email-otp/validate"):
                return _Resp(200, {"page": {"type": "email_otp_verification"}, "continue_url": "https://chatgpt.com/"})
            raise AssertionError(f"unexpected POST {url}")

        driver = _Mock()
        driver.execute_script.return_value = {"url": "https://auth.openai.com/email-verification", "inputs": [{"name": "code", "type": "text"}]}
        session = _Mock()

        with _patch.object(pr, "time") as fake_time, _patch(
            "core.cloakbrowser_driver.build_cloak_driver", return_value=(driver, _Mock())
        ), _patch("core.page_session.PageSession", return_value=session), _patch(
            "core.chatgpt_auth.signin_openai", return_value="https://auth.openai.com/api/accounts/authorize?x=1"
        ), _patch("core.codex_oauth._post_json", side_effect=fake_post), _patch(
            "core.email_provider.wait_for_otp", return_value="123456"
        ), _patch(
            "core.account_export.fetch_session", return_value={"accessToken": "AT-1", "user": {}, "account": {}}
        ), _patch("core.account_export.save_account_data", return_value=7):
            fake_time.sleep = lambda *_: None
            fake_time.time = lambda: 0.0
            result = pr.run_protocol_registration("fresh@icloud.com")

        self.assertTrue(result["ok"], result)
        self.assertFalse([u for u in calls if u.endswith("/authorize/continue")])

    def test_otp_only_branch_does_not_touch_user_register(self):
        result, calls, saved, _session = self._run(page_type="email_otp_verification")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["registration_password"], None)
        self.assertFalse([url for url, _ in calls if url.endswith("/user/register")])
        self.assertNotIn("registration_password", saved["extra"])


if __name__ == "__main__":
    unittest.main()