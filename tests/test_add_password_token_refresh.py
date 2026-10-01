# -*- coding: utf-8 -*-
"""回归：补密码成功后必须立刻协议刷新 AT/RT 并回写（run50c 改密吊销事件）。

改密（reset-password）会吊销改密前写回的 access_token；如果补密码服务不在
成功后立刻刷新，收尾链路会在检查时报 401 token_revoked（"账号尚未测活成功"）。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.add_password_service as svc


class RefreshAfterPasswordTests(unittest.TestCase):
    def test_run_triggers_refresh_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("core.account_password.add_password_protocol",
                            return_value={"ok": True, "status": "updated", "email": "a@b.com", "password": "pw"}), \
                 mock.patch("core.db.set_account_password_pending") as pending, \
                 mock.patch.object(svc, "log_path", side_effect=lambda e: Path(td) / f"log-{e}.log"), \
                 mock.patch.object(svc, "_refresh_tokens_after_password") as refresh:
                result = svc._run("a@b.com", "unit-test")
        self.assertTrue(result.get("ok"))
        pending.assert_called_once()
        refresh.assert_called_once_with("a@b.com")

    def test_run_skips_refresh_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("core.account_password.add_password_protocol",
                            return_value={"ok": False, "status": "failed", "email": "a@b.com", "error": "boom"}), \
                 mock.patch("core.db.get_account_by_email", return_value={"password_pending_attempts": 1}), \
                 mock.patch("core.db.set_account_password_pending"), \
                 mock.patch.object(svc, "log_path", side_effect=lambda e: Path(td) / f"log-{e}.log"), \
                 mock.patch.object(svc, "_refresh_tokens_after_password") as refresh:
                result = svc._run("a@b.com", "unit-test")
        self.assertFalse(result.get("ok"))
        refresh.assert_not_called()

    def test_refresh_helper_retries_and_writes_back(self):
        calls = []

        def fake_login(email, password, **kw):
            calls.append((email, password, kw))
            if len(calls) == 1:
                return {"ok": False, "error": "transient"}
            return {"ok": True, "elapsed_ms": 5}

        with mock.patch("core.db.get_account_by_email",
                        return_value={"password": "pw", "totp_secret": "", "id": 7}), \
             mock.patch("core.password_login.login_with_password", side_effect=fake_login), \
             mock.patch("core.live_check_service._young_account_country_hint", return_value="US"), \
             mock.patch.object(svc.time, "sleep"):
            svc._refresh_tokens_after_password("a@b.com")
        self.assertEqual(len(calls), 2, "首次失败应重试一次")
        self.assertTrue(all(kw.get("write_back") for _, _, kw in calls))
        self.assertEqual(calls[0][1], "pw")


if __name__ == "__main__":
    unittest.main()
