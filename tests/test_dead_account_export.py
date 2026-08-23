# -*- coding: utf-8 -*-
import tempfile
import unittest
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db, icloud_mail_client
from core.account_liveness import classify_liveness_failure
from core.openai_auth import AccountUnusableError
from webui.app import create_app


class DeadAccountExportTests(unittest.TestCase):
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
        self.icloud_file = root / "icloud_mailboxes.txt"
        self.icloud_state = root / "icloud_mailboxes.json"
        from config import email as email_config
        self.stack.enter_context(patch.object(email_config, "ICLOUD_MAILBOXES_FILE", str(self.icloud_file)))
        self.stack.enter_context(patch.object(icloud_mail_client, "_STATE_FILE", self.icloud_state))

    def test_network_and_http_failures_are_temporary_but_explicit_dead_code_is_confirmed(self):
        for exc in (
            RuntimeError("403 Forbidden"),
            RuntimeError("429 Too Many Requests"),
            RuntimeError("HTTP 500 upstream error"),
            RuntimeError("HTTP 599 upstream error"),
            TimeoutError("request timed out"),
            RuntimeError("SOCKS proxy connection reset"),
        ):
            with self.subTest(exc=exc):
                result = classify_liveness_failure(exc)
                self.assertEqual(result["status"], "temporary_error")
                self.assertFalse(result["ok"])

        dead = classify_liveness_failure(
            AccountUnusableError("account removed", error_code="account_deleted")
        )
        self.assertEqual(dead["status"], "confirmed_dead")
        self.assertEqual(dead["error"], "account_deleted")

        for exc in (
            AccountUnusableError("HTTP 403 account removed", error_code="account_deleted"),
            AccountUnusableError("proxy timeout", error_code="account_deactivated"),
        ):
            with self.subTest(precedence=exc):
                self.assertEqual(classify_liveness_failure(exc)["status"], "temporary_error")

    def test_confirmed_dead_filter_and_txt_export_are_one_email_per_line(self):
        dead_ids = []
        for email in ("dead-one@example.com", "dead-two@example.com"):
            acc_id = db.insert_account(email=email, access_token=f"token-{email}")
            db.update_account_liveness(acc_id, {
                "ok": False,
                "status": "confirmed_dead",
                "error": "account_deleted",
            })
            dead_ids.append(acc_id)
        live_id = db.insert_account(email="live@example.com", access_token="live-token")
        db.update_account_liveness(live_id, {"ok": True, "status": "live"})

        filtered = db.list_accounts_page(status_filter="confirmed_dead", limit=50)
        self.assertEqual({row["id"] for row in filtered["items"]}, set(dead_ids))

        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        listed = client.get("/api/accounts?paged=1&status=confirmed_dead")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual({row["email"] for row in listed.get_json()["items"]}, {
            "dead-one@example.com", "dead-two@example.com"
        })

        exported = client.get("/api/accounts/confirmed-dead.txt")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["Content-Disposition"])
        self.assertEqual(
            set(exported.get_data(as_text=True).splitlines()),
            {"dead-one@example.com", "dead-two@example.com"},
        )

    def test_oauth_status_filter_separates_complete_and_access_only_accounts(self):
        complete = db.insert_account(email="complete@example.com", access_token="complete-token")
        db.update_account_chatgpt_oauth("complete@example.com", {
            "email": "complete@example.com",
            "access_token": "complete-token-new",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        })
        access_only = db.insert_account(email="access-only@example.com", access_token="access-only-token")

        complete_rows = db.list_accounts_page(oauth_filter="complete", limit=50)["items"]
        access_only_rows = db.list_accounts_page(oauth_filter="access_only", limit=50)["items"]
        self.assertEqual({row["id"] for row in complete_rows}, {complete})
        self.assertEqual({row["id"] for row in access_only_rows}, {access_only})

    def test_ambiguous_invalid_requires_a_second_check_after_interval(self):
        acc_id = db.insert_account(email="review@example.com", access_token="review-token")
        candidate = classify_liveness_failure(RuntimeError("invalid_account"))
        self.assertEqual(candidate["status"], "temporary_error")
        self.assertTrue(candidate["dead_candidate"])

        db.update_account_liveness(acc_id, candidate)
        self.assertEqual(db.get_account(acc_id)["pipeline_status"], "temporary_error")

        rows = json.loads(Path(db._ACCOUNTS_JSON).read_text(encoding="utf-8"))
        rows[0]["dead_candidate_checked_at"] = "2026-08-11T00:00:00"
        Path(db._ACCOUNTS_JSON).write_text(json.dumps(rows), encoding="utf-8")
        db.update_account_liveness(acc_id, candidate)

        stored = db.get_account(acc_id)
        self.assertEqual(stored["live_check_status"], "confirmed_dead")
        self.assertEqual(stored["pipeline_status"], "confirmed_dead")

    def test_disabling_icloud_alias_persists_disabled_pool_state(self):
        icloud_mail_client.import_mailboxes("alias@icloud.com----测试")

        icloud_mail_client.release_account(
            "alias@icloud.com", status="disabled", note="Apple 控制台已停用"
        )

        row = icloud_mail_client.list_mailboxes()[0]
        self.assertEqual(row["status"], "disabled")
        self.assertIn("停用", row["note"])


if __name__ == "__main__":
    unittest.main()
