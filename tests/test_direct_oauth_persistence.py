# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from config import codex as codex_config
from core import codex_oauth, codex_retry_service, db


class DirectOAuthPersistenceTests(unittest.TestCase):
    def test_authorize_url_uses_chatgpt2api_oauth_parameters(self):
        with patch.object(codex_config, "CODEX_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"), patch.object(
            codex_config, "CODEX_AUTH_URL", "https://auth.openai.com/api/accounts/authorize"
        ), patch.object(codex_config, "CODEX_REDIRECT_URI", "https://platform.openai.com/auth/callback"):
            url = codex_oauth._build_authorize_url("state-fixture", "challenge-fixture")

        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["app_2SKx67EdpoN0G6j64rFvigXD"])
        self.assertEqual(query["redirect_uri"], ["https://platform.openai.com/auth/callback"])
        self.assertEqual(query["audience"], ["https://api.openai.com/v1"])
        self.assertEqual(query["state"], ["state-fixture"])
        self.assertEqual(query["code_challenge"], ["challenge-fixture"])

    def test_platform_callback_is_accepted_by_browser_flow(self):
        from core import roxy_codex_oauth

        self.assertTrue(roxy_codex_oauth._is_callback_url(
            "https://platform.openai.com/auth/callback?code=CODE&state=STATE"
        ))

    def test_oauth_file_is_persisted_before_plan_check_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with ExitStack() as stack:
                for name, value in {
                    "_ACCOUNTS_JSON": root / "accounts.json",
                    "_LEGACY_ACCOUNTS_JSON": root / "legacy.json",
                    "_ACCOUNTS_TXT": root / "accounts.txt",
                    "_TOKENS_TXT": root / "tokens.txt",
                    "_OUTLOOK_JSON": root / "outlook.json",
                    "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
                    "_OUTLOOK_TXT": root / "outlook.txt",
                    "_VIEWER_HTML": root / "viewer.html",
                    "_LOG_DIR": root / "logs",
                }.items():
                    stack.enter_context(patch.object(db, name, value))
                stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
                account_id = db.insert_account(email="alias@icloud.com", access_token="old-access-token")
                credential_path = root / "codex_alias.json"
                credential_path.write_text(json.dumps({
                    "type": "codex",
                    "email": "alias@icloud.com",
                    "access_token": "new-access-token",
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "account_id": "account-1",
                    "expired": "2026-09-01T00:00:00Z",
                }), encoding="utf-8")
                plan_queue = Mock(return_value={"accepted": True, "status": "queued"})
                oauth_result = {"ok": True, "status": "success", "file_path": str(credential_path)}
                with patch.object(codex_retry_service, "_select_oauth_route", return_value={
                    "proxy_url": "http://HOST:PORT", "mode": "mihomo_excluded"
                }), patch.object(codex_retry_service, "db", db), patch(
                    "config.reload_all"
                ), patch(
                    "core.codex_oauth.run_codex_oauth", return_value=oauth_result
                ), patch(
                    "core.plan_check_service.enqueue_account_plan_check", plan_queue
                ):
                    result = codex_retry_service.run_worker(
                        "alias@icloud.com",
                        target_log_path=root / "retry.log",
                    )

                stored = db.get_account(account_id)
                self.assertTrue(result["ok"])
                self.assertEqual(stored["refresh_token"], "refresh-token")
                self.assertEqual(stored["id_token"], "id-token")
                self.assertEqual(stored["codex_status"], "success")
                self.assertEqual(stored["oauth_status"], "success")
                plan_queue.assert_called_once_with(
                    account_id=account_id,
                    email="alias@icloud.com",
                    access_token="new-access-token",
                    trigger="oauth_persisted",
                )


if __name__ == "__main__":
    unittest.main()
