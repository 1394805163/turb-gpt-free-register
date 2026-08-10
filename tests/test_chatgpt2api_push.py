# -*- coding: utf-8 -*-
import logging
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from config import chatgpt2api as push_config
from core import db
from core.account_export import fetch_session
from core.chatgpt2api_push import push_account, token_fingerprint


class Chatgpt2ApiPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_VIEWER_HTML": root / "viewer.html",
            "_LOG_DIR": root / "logs",
        }.items():
            self.stack.enter_context(patch.object(db, name, value))
        self.stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
        self.account_id = db.insert_account(
            email="live@example.com",
            access_token="secret-access-token",
            user_id="user-1",
            plan_type="free",
            extra={"account": {"id": "acct-1", "planType": "free"}},
        )
        db.update_account_liveness(self.account_id, {
            "ok": True,
            "status": "live",
            "checked_at": "2026-08-11T10:00:00",
            "access_token": "secret-access-token",
        })
        for name, value in {
            "CHATGPT2API_PUSH_ENABLED": True,
            "CHATGPT2API_BASE_URL": "http://TARGET:PORT",
            "CHATGPT2API_ADMIN_KEY": "admin-secret",
            "CHATGPT2API_TIMEOUT": 3.0,
            "CHATGPT2API_MAX_RETRIES": 3,
            "CHATGPT2API_BACKOFF_BASE": 0.25,
        }.items():
            self.stack.enter_context(patch.object(push_config, name, value))

    @staticmethod
    def _response(status=200, payload=None):
        response = Mock()
        response.status_code = status
        response.text = "response without secrets"
        response.json.return_value = payload or {"ok": True}
        return response

    @patch("core.chatgpt2api_push.requests.post")
    def test_posts_complete_account_and_is_idempotent_for_same_token(self, post):
        post.return_value = self._response()

        first = push_account(self.account_id, sleep_fn=lambda _seconds: None)
        second = push_account(self.account_id, sleep_fn=lambda _seconds: None)

        self.assertTrue(first["ok"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], "http://TARGET:PORT/api/accounts")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer admin-secret")
        self.assertFalse(kwargs["json"]["refresh_after_import"])
        self.assertEqual(len(kwargs["json"]["accounts"]), 1)
        sent = kwargs["json"]["accounts"][0]
        self.assertEqual(sent["id"], self.account_id)
        self.assertEqual(sent["email"], "live@example.com")
        self.assertEqual(sent["access_token"], "secret-access-token")

        stored = db.get_account(self.account_id)
        self.assertEqual(stored["pipeline_status"], "pushed")
        self.assertEqual(stored["push_status"], "pushed")
        self.assertEqual(stored["push_token_fingerprint"], token_fingerprint("secret-access-token"))
        self.assertNotIn("secret-access-token", stored["push_token_fingerprint"])

    @patch("core.chatgpt2api_push.requests.post")
    def test_retries_transient_errors_with_exponential_backoff_without_logging_token(self, post):
        post.side_effect = [
            self._response(503),
            requests.Timeout("timeout while sending secret-access-token"),
            self._response(200),
        ]
        sleeps = []

        with self.assertLogs("core.chatgpt2api_push", level=logging.INFO) as captured:
            result = push_account(self.account_id, sleep_fn=sleeps.append)

        self.assertTrue(result["ok"])
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertNotIn("secret-access-token", "\n".join(captured.output))
        self.assertEqual(db.get_account(self.account_id)["push_attempts"], 3)

    @patch("core.chatgpt2api_push.requests.post")
    def test_does_not_push_before_successful_liveness(self, post):
        pending_id = db.insert_account(
            email="pending@example.com",
            access_token="pending-token",
        )

        result = push_account(pending_id, sleep_fn=lambda _seconds: None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "pending")
        post.assert_not_called()

    def test_interrupted_push_is_recovered_for_restart_retry(self):
        account = db.get_account(self.account_id)
        fingerprint = token_fingerprint(account["access_token"])
        self.assertEqual(db.claim_account_push(self.account_id, fingerprint), "claimed")

        recovered = db.recover_interrupted_account_pushes()

        self.assertEqual(recovered, 1)
        stored = db.get_account(self.account_id)
        self.assertEqual(stored["push_status"], "push_failed")
        self.assertEqual(stored["pipeline_status"], "live")
        self.assertIn(self.account_id, db.list_push_candidate_ids())

    def test_enabled_push_with_incomplete_config_persists_push_failed(self):
        with patch.object(push_config, "CHATGPT2API_BASE_URL", ""), patch.object(
            push_config, "CHATGPT2API_ADMIN_KEY", ""
        ):
            result = push_account(self.account_id, sleep_fn=lambda _seconds: None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "push_failed")
        stored = db.get_account(self.account_id)
        self.assertEqual(stored["pipeline_status"], "push_failed")
        self.assertEqual(stored["push_status"], "push_failed")
        self.assertEqual(stored["push_attempts"], 0)

    def test_session_error_log_does_not_echo_other_token_fields(self):
        session = Mock()
        session.get_nextauth_headers.return_value = {}
        response = Mock()
        response.json.return_value = {"refreshToken": "other-secret-token", "user": {"id": "u-1"}}
        session.get.return_value = response

        with self.assertLogs("core.account_export", level=logging.ERROR) as captured:
            with self.assertRaises(RuntimeError):
                fetch_session(session)

        self.assertNotIn("other-secret-token", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
