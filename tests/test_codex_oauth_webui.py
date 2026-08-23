# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from webui import app as webui_app


def _account(account_id: int, email: str, *, created_at: str, token_expires_at: str, token_expired: bool):
    return {
        "id": account_id,
        "email": email,
        "access_token": f"access-{account_id}",
        "created_at": created_at,
        "token_expires_at": token_expires_at,
        "token_expired": token_expired,
        "chatgpt_refresh_token": "",
        "codex_status": "skipped",
    }


class CodexOAuthWebUiTests(unittest.TestCase):
    def setUp(self):
        self.app = webui_app.create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_early_oauth_request_is_converted_to_simple_plan_check(self):
        account = _account(
            1,
            "new@example.com",
            created_at="2026-08-22T12:00:00+00:00",
            token_expires_at="2026-08-30T12:00:00+00:00",
            token_expired=False,
        )
        queued = {"accepted": True, "status": "queued", "account_id": 1}
        with patch.object(webui_app.db, "get_account_by_email", return_value=account), patch.object(
            webui_app.plan_check_service,
            "enqueue_account_plan_check",
            return_value=queued,
        ) as enqueue, patch.object(webui_app.codex_retry_service, "reserve") as reserve:
            response = self.client.post(
                "/api/codex/retry",
                headers=self.headers,
                json={"email": account["email"]},
            )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["action"], "plan_check")
        self.assertEqual(body["eligibility"]["reason_code"], "account_too_new")
        enqueue.assert_called_once()
        reserve.assert_not_called()

    def test_bulk_oauth_only_starts_eligible_accounts_and_plan_checks_others(self):
        old = _account(
            1,
            "old@example.com",
            created_at="2026-08-12T12:00:00+00:00",
            token_expires_at="2026-08-21T12:00:00+00:00",
            token_expired=True,
        )
        new = _account(
            2,
            "new@example.com",
            created_at="2026-08-22T12:00:00+00:00",
            token_expires_at="2026-08-30T12:00:00+00:00",
            token_expired=False,
        )
        accounts = {1: old, 2: new}
        queued = {"accepted": True, "status": "queued", "account_id": 2}
        with patch.object(webui_app.db, "get_account", side_effect=lambda account_id: accounts.get(int(account_id))), patch.object(
            webui_app.codex_retry_service, "reserve", return_value=True
        ), patch.object(
            webui_app.plan_check_service,
            "enqueue_account_plan_check",
            return_value=queued,
        ) as enqueue, patch.object(webui_app.threading, "Thread") as thread:
            response = self.client.post(
                "/api/codex/retry-bulk",
                headers=self.headers,
                json={"account_ids": [1, 2], "workers": 2},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["started_count"], 1)
        self.assertEqual(body["simple_check_started_count"], 1)
        self.assertEqual(body["started"][0]["id"], 1)
        self.assertEqual(body["simple_check_started"][0]["id"], 2)
        enqueue.assert_called_once()
        thread.assert_called_once()

    def test_account_toolbar_uses_clicked_button_for_live_check_and_distinguishes_oauth(self):
        root = Path(__file__).resolve().parent.parent
        template = (root / "webui/templates/index.html").read_text(encoding="utf-8")
        self.assertEqual(template.count('id="btnCheckSelectedLiveTopV2"'), 0)
        self.assertEqual(template.count('id="btnCheckSelectedLiveV2"'), 1)
        self.assertIn("'btnCheckSelectedLiveV2'", template)
        self.assertIn("批量查活/刷新 AT", template)
        self.assertIn("OAuth持久化（满7天）", template)
        legacy = (root / "webui/templates/index_legacy.html").read_text(encoding="utf-8")
        self.assertEqual(legacy.count('id="btnCheckSelectedLiveTop"'), 0)
        self.assertEqual(legacy.count('id="btnCheckSelectedLive"'), 1)

    def test_token_check_button_is_explicitly_separate_from_email_refresh(self):
        template = (Path(__file__).resolve().parent.parent / "webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("使用现有 Token 快速测活并查询套餐", template)
        self.assertIn("不访问 /api/auth/providers", template)

    def test_live_check_does_not_open_stale_log_when_nothing_was_queued(self):
        template = (Path(__file__).resolve().parent.parent / "webui/templates/index.html").read_text(encoding="utf-8")
        block = template[template.index("async function checkSelectedLive"):template.index("async function deleteAccount")]
        self.assertIn("if (firstStarted?.email) openLiveLog(firstStarted.email);", block)
        self.assertNotIn("else if (firstAcc && firstAcc.email) openLiveLog(firstAcc.email);", block)


if __name__ == "__main__":
    unittest.main()
