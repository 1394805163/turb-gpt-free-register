# -*- coding: utf-8 -*-
"""协议 RT 刷新（core/oauth_refresh）：缺凭据 / 成功+写回 / HTTP 错误。"""
import json
import unittest
from unittest.mock import Mock, patch

from core import oauth_refresh


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class OAuthRefreshTests(unittest.TestCase):
    def _acc(self, **kw):
        base = {
            "chatgpt_refresh_token": "rt-fixture",
            "chatgpt_oauth_client_id": "app_fixture",
            "chatgpt_oauth_access_token": "at-old",
            "chatgpt_id_token": "id-old",
        }
        base.update(kw)
        return base

    def test_missing_credentials(self):
        with patch("core.db.get_account_by_email", return_value={}):
            res = oauth_refresh.refresh_account_credentials("fixture@example.com")
        self.assertFalse(res["ok"])
        self.assertIn("缺少", res["error"])

    def test_success_with_write_back(self):
        resp = _Resp(200, {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "id-new"})
        session = Mock()
        session.post.return_value = resp
        with patch("core.db.get_account_by_email", return_value=self._acc()), patch(
            "core.db.update_account_chatgpt_oauth", return_value={"updated": True}
        ) as wb, patch("curl_cffi.requests.Session", return_value=session):
            res = oauth_refresh.refresh_account_credentials("fixture@example.com")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["rt_rotated"])
        self.assertEqual(res["access_token"], "at-new")
        wb.assert_called_once()

    def test_http_error(self):
        resp = _Resp(401, {"error": "invalid_grant", "error_description": "bad token"})
        session = Mock()
        session.post.return_value = resp
        with patch("core.db.get_account_by_email", return_value=self._acc()), patch(
            "curl_cffi.requests.Session", return_value=session
        ):
            res = oauth_refresh.refresh_account_credentials("fixture@example.com", write_back=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 401)
        self.assertIn("bad token", res["error"])


if __name__ == "__main__":
    unittest.main()
