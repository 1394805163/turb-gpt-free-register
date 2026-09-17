# -*- coding: utf-8 -*-
"""导出多格式（完整 / sub2api / CPA / 纯 AT）与纯 AT 文本导入。"""
import json
import unittest
from unittest.mock import patch

from webui.app import create_app


def _rows():
    return {
        1: {
            "id": 1,
            "email": "full@example.com",
            "password": "Pw-1",
            "totp_secret": "TOTP-1",
            "chatgpt_oauth_access_token": "AT-1",
            "chatgpt_refresh_token": "RT-1",
            "chatgpt_id_token": "ID-1",
            "chatgpt_account_id": "ACC-1",
            "chatgpt_oauth_client_id": "CID-1",
            "chatgpt_token_expires_at": "2026-10-01T00:00:00Z",
            "plan_type": "free",
            "live_check_status": "live",
            "archived": False,
        },
        2: {
            "id": 2,
            "email": "atlonly@example.com",
            "access_token": "AT-ONLY-LONG-" + "x" * 90,
            "archived": False,
        },
    }


class ExportFormatTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def _export(self, fmt: str) -> str:
        with patch("webui.app.db.get_account", side_effect=lambda account_id: _rows().get(int(account_id))):
            response = self.client.post(
                "/api/accounts/export-json-bulk",
                headers=self.headers,
                json={"account_ids": [1, 2], "prepare": True, "format": fmt},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:200])
        prepared = response.get_json()
        download = self.client.get(prepared["download_url"], headers=self.headers)
        self.assertEqual(download.status_code, 200)
        return download.get_data(as_text=True)

    def test_multi_account_format_keeps_password_and_totp(self):
        payload = json.loads(self._export("multi_account_v1"))
        self.assertEqual(payload["format"], "multi_account_v1")
        first = payload["accounts"][0]
        self.assertEqual(first["password"], "Pw-1")
        self.assertEqual(first["totp_secret"], "TOTP-1")
        self.assertEqual(first["refresh_token"], "RT-1")

    def test_sub2api_format_shape(self):
        payload = json.loads(self._export("sub2api"))
        entry = payload["accounts"][0]
        self.assertEqual(entry["platform"], "openai")
        self.assertEqual(entry["type"], "codex")
        self.assertEqual(entry["credentials"]["access_token"], "AT-1")
        self.assertEqual(entry["extra"]["email"], "full@example.com")

    def test_cpa_format_shape(self):
        payload = json.loads(self._export("cpa"))
        entry = payload["accounts"][0]
        self.assertEqual(entry["type"], "codex")
        self.assertEqual(entry["id_token"], "ID-1")
        self.assertTrue(entry["access_token"])

    def test_access_token_text_format(self):
        text = self._export("access_token")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("full@example.com----AT-1"))
        self.assertIn("atlonly@example.com----AT-ONLY-", lines[1])

    def test_at_only_text_import_goes_through_credentials(self):
        captured = {}

        def fake_import(records, source=None):
            captured["records"] = records
            return {"inserted": 1, "updated": 0, "skipped": 0, "errors": []}

        token = "eyJhbGciOiJSUzI1NiJ9." + "a" * 80 + ".signature"
        with patch("core.db.import_account_credentials", side_effect=fake_import):
            response = self.client.post(
                "/api/accounts/import-password-login",
                headers=self.headers,
                json={"text": f"atlonly@example.com----{token}\n"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body.get("queued"), 0)
        self.assertEqual(body.get("access_token_imported"), 1)
        self.assertEqual(captured["records"][0]["email"], "atlonly@example.com")
        self.assertEqual(captured["records"][0]["access_token"], token)

    def test_password_line_still_goes_to_protocol_login(self):
        with patch("threading.Thread") as thr:
            response = self.client.post(
                "/api/accounts/import-password-login",
                headers=self.headers,
                json={"text": "full@example.com----Pw-1----TOTP-1\n"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("queued"), 1)
        self.assertTrue(thr.called)


if __name__ == "__main__":
    unittest.main()
