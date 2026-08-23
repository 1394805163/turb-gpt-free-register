# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.account_export import save_account_data
from core.codex_oauth import save_codex_credential


class ChatGPTOAuthCredentialTests(unittest.TestCase):
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
        }.items():
            self.stack.enter_context(patch.object(db, name, value))
        self.stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
        self.stack.enter_context(
            patch(
                "core.plan_check_service.enqueue_account_plan_check",
                return_value={"accepted": False, "error": "fixture"},
            )
        )
        self.root = root

    def _write_mailbox_pool(self) -> None:
        (self.root / "outlook.json").write_text(
            json.dumps([{
                "id": 1,
                "email": "alias@example.com",
                "password": "mail-password",
                "client_id": "microsoft-client",
                "refresh_token": "mail-refresh-token",
                "status": "available",
            }]),
            encoding="utf-8",
        )

    def test_registration_keeps_mailbox_refresh_token_separate_from_chatgpt_oauth(self):
        self._write_mailbox_pool()
        credential_path = self.root / "codex-alias.json"
        credential_path.write_text(json.dumps({
            "type": "codex",
            "access_token": "oauth-access-token",
            "refresh_token": "chatgpt-refresh-token",
            "id_token": "chatgpt-id-token",
            "oauth_client_id": "codex-client",
        }), encoding="utf-8")

        row_id = save_account_data(
            "alias@example.com",
            "web-session-access-token",
            extra={
                "account": {"planType": "free"},
                "codex": {"status": "success", "file_path": str(credential_path)},
            },
            email_source="outlook",
            batch_dir=self.root / "batch",
        )

        row = db.get_account(row_id)
        self.assertEqual(row["refresh_token"], "mail-refresh-token")
        self.assertEqual(row["client_id"], "microsoft-client")
        self.assertEqual(row["chatgpt_oauth_access_token"], "oauth-access-token")
        self.assertEqual(row["chatgpt_refresh_token"], "chatgpt-refresh-token")
        self.assertEqual(row["chatgpt_id_token"], "chatgpt-id-token")
        self.assertEqual(row["chatgpt_oauth_client_id"], "codex-client")
        self.assertNotIn("chatgpt-refresh-token", row["copy_line"])

    def test_existing_account_receives_oauth_credentials_and_updates_current_access_token(self):
        row_id = db.insert_account(email="alias@icloud.com", access_token="old-access", email_source="icloud")
        result = db.update_account_chatgpt_oauth("alias@icloud.com", {
            "type": "codex",
            "email": "alias@icloud.com",
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "id_token": "oauth-id-token",
            "oauth_client_id": "codex-client",
            "account_id": "chatgpt-account-id",
            "expired": "2026-08-23T00:00:00Z",
            "last_refresh": "2026-08-22T00:00:00Z",
        })
        self.assertTrue(result["updated"])
        self.assertEqual(result["account_id"], row_id)
        row = db.get_account(row_id)
        self.assertEqual(row["access_token"], "oauth-access-token")
        self.assertEqual(row["refresh_token"], "oauth-refresh-token")
        self.assertEqual(row["id_token"], "oauth-id-token")
        self.assertEqual(row["oauth_client_id"], "codex-client")
        self.assertEqual(row["account_id"], "chatgpt-account-id")

    def test_import_oauth_upserts_account_and_marks_mailbox_used(self):
        with patch("core.email_provider.release_email") as release_email:
            result = db.import_chatgpt_oauth_credentials([{
                "type": "codex",
                "email": "new-alias@icloud.com",
                "access_token": "oauth-access-token",
                "refresh_token": "oauth-refresh-token",
                "id_token": "oauth-id-token",
                "account_id": "chatgpt-account-id",
                "expired": "2026-08-23T00:00:00Z",
            }])

        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["updated"], 0)
        row = db.get_account_by_email("new-alias@icloud.com")
        self.assertEqual(row["chatgpt_refresh_token"], "oauth-refresh-token")
        self.assertEqual(row["oauth_status"], "success")
        release_email.assert_called_once()
        self.assertEqual(release_email.call_args.kwargs["status"], "used")

    def test_import_account_credentials_accepts_access_token_only(self):
        result = db.import_account_credentials([{
            "email": "access-only@icloud.com",
            "access_token": "access-only-token",
            "email_source": "icloud",
        }])
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["oauth_status"], {"access_only": 1})
        row = db.get_account_by_email("access-only@icloud.com")
        self.assertEqual(row["access_token"], "access-only-token")
        self.assertFalse(row.get("oauth_status"))

    def test_import_account_credentials_uses_chatgpt_oauth_fields_to_overwrite_pool(self):
        row_id = db.insert_account(
            email="overwrite@icloud.com",
            access_token="stale-access-token",
            email_source="icloud",
        )
        with patch("core.email_provider.release_email") as release_email:
            result = db.import_account_credentials([{
                "email": "overwrite@icloud.com",
                "access_token": "stale-access-token",
                "chatgpt_oauth_access_token": "oauth-access-token",
                "chatgpt_refresh_token": "oauth-refresh-token",
                "chatgpt_id_token": "oauth-id-token",
                "chatgpt_oauth_client_id": "oauth-client",
                "chatgpt_account_id": "chatgpt-account-id",
                "email_source": "icloud",
            }])

        self.assertEqual(result["oauth_status"], {"complete": 1})
        self.assertEqual(result["updated"], 1)
        row = db.get_account(row_id)
        self.assertEqual(row["access_token"], "oauth-access-token")
        self.assertEqual(row["chatgpt_refresh_token"], "oauth-refresh-token")
        self.assertEqual(row["refresh_token"], "oauth-refresh-token")
        self.assertEqual(row["id_token"], "oauth-id-token")
        self.assertEqual(row["oauth_client_id"], "oauth-client")
        self.assertEqual(row["account_id"], "chatgpt-account-id")
        release_email.assert_called_once()
        self.assertEqual(release_email.call_args.kwargs["status"], "used")

    def test_save_codex_credential_normalizes_cpa_aliases(self):
        with patch("core.codex_oauth._PROJECT_ROOT", self.root):
            with patch("core.codex_oauth._cfg.CODEX_OUTPUT_DIRNAME", "codex_accounts"):
                path = save_codex_credential({
                    "client_id": "cpa-client",
                    "accessToken": "oauth-access-token",
                    "refreshToken": "oauth-refresh-token",
                    "idToken": "oauth-id-token",
                    "account_id": "chatgpt-account-id",
                }, "alias@icloud.com", "free")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["type"], "codex")
        self.assertEqual(saved["oauth_client_id"], "cpa-client")
        self.assertEqual(saved["access_token"], "oauth-access-token")
        self.assertEqual(saved["refresh_token"], "oauth-refresh-token")
        self.assertEqual(saved["id_token"], "oauth-id-token")


if __name__ == "__main__":
    unittest.main()
