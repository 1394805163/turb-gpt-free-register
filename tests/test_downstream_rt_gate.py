# -*- coding: utf-8 -*-
"""回归：AT-only 推送（下游无 RT）时，本地必须自己刷新，而不是无限“等下游”。

背景：9/23-24、10/01 批次为 AT-only 推送（下游 RT 缺失）。旧逻辑对“已托管下游”
账号一律本地不碰 RT/不做刷新（等下游续期），下游却无 RT 无法续期 → 整批 AT 失效。
修复：仅当下游确实持有可续期 RT（downstream_rt_status=valid/ok）时才托管；
否则继续走本地刷新（RT 快路径 / 密码+2FA 协议登录）。
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import core.account_liveness as liveness
import core.oauth_refresh as oauth_refresh


def _acc(**over):
    base = {
        "email": "user@example.com",
        "push_status": "pushed",
        "downstream_rt_status": "missing",
        "access_token": "expired-token",
        "password": "pw",
        "totp_secret": "TOTP",
        "chatgpt_refresh_token": "",
        "chatgpt_oauth_client_id": "",
    }
    base.update(over)
    return base


class LivenessDownstreamRtGateTests(unittest.TestCase):
    def test_at_only_account_refreshes_locally(self):
        with mock.patch("core.db.get_account_by_email", return_value=_acc()), \
             mock.patch("core.chatgpt_plan.token_claims", return_value={"token_expired": True}), \
             mock.patch("core.password_login.login_with_password",
                        return_value={"ok": True, "access_token": "new-at", "refresh_token": "new-rt", "elapsed_ms": 5}) as login:
            res = liveness._protocol_fast_path("user@example.com")
        self.assertTrue(res and res.get("ok"))
        self.assertEqual(res.get("method"), "password_login")
        login.assert_called_once()

    def test_downstream_rt_valid_still_defers(self):
        with mock.patch("core.db.get_account_by_email", return_value=_acc(downstream_rt_status="valid")), \
             mock.patch("core.chatgpt_plan.token_claims", return_value={"token_expired": True}), \
             mock.patch("core.password_login.login_with_password") as login:
            res = liveness._protocol_fast_path("user@example.com")
        self.assertTrue(res and not res.get("ok"))
        self.assertEqual(res.get("method"), "downstream_managed")
        login.assert_not_called()

    def test_not_pushed_account_refreshes_locally(self):
        with mock.patch("core.db.get_account_by_email", return_value=_acc(push_status="")), \
             mock.patch("core.chatgpt_plan.token_claims", return_value={"token_expired": True}), \
             mock.patch("core.password_login.login_with_password",
                        return_value={"ok": True, "access_token": "new-at", "refresh_token": "new-rt", "elapsed_ms": 5}) as login:
            res = liveness._protocol_fast_path("user@example.com")
        self.assertTrue(res and res.get("ok"))
        login.assert_called_once()


class OauthRefreshDownstreamRtGateTests(unittest.TestCase):
    def test_oauth_refresh_allows_at_only_pushed(self):
        acc = _acc(chatgpt_refresh_token="rt-1", chatgpt_oauth_client_id="cid-1")
        resp = SimpleNamespace(status_code=200, text="{}", json=lambda: {"access_token": "new-at", "refresh_token": "new-rt", "id_token": "idt"})
        sess = mock.MagicMock()
        sess.post.return_value = resp
        with mock.patch("core.db.get_account_by_email", return_value=acc), \
             mock.patch("core.db.update_account_chatgpt_oauth", return_value={"updated": True}), \
             mock.patch("curl_cffi.requests.Session", return_value=sess):
            res = oauth_refresh.refresh_account_credentials("user@example.com")
        self.assertTrue(res.get("ok"))
        sess.post.assert_called_once()

    def test_oauth_refresh_blocks_when_downstream_has_rt(self):
        acc = _acc(downstream_rt_status="valid", chatgpt_refresh_token="rt-1", chatgpt_oauth_client_id="cid-1")
        with mock.patch("core.db.get_account_by_email", return_value=acc), \
             mock.patch("curl_cffi.requests.Session") as sess_cls:
            res = oauth_refresh.refresh_account_credentials("user@example.com")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("status"), "downstream_managed")
        sess_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
