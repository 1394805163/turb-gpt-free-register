# -*- coding: utf-8 -*-
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from webui.app import create_app


class OAuthExportWebUiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_selected_oauth_export_contains_complete_persisted_credential(self):
        rows = {
            12: {
                "id": 12,
                "email": "alias@example.com",
                "oauth_status": "success",
                "chatgpt_oauth_access_token": "oauth-access",
                "chatgpt_refresh_token": "oauth-refresh",
                "chatgpt_id_token": "oauth-id",
                "chatgpt_account_id": "account-12",
                "chatgpt_token_expires_at": "2026-09-01T00:00:00Z",
                "last_refresh": "2026-08-23T00:00:00+00:00",
                "chatgpt_oauth_client_id": "client-12",
                "archived": False,
                "codex_status": "success",
            },
            13: {
                "id": 13,
                "email": "missing@example.com",
                "oauth_status": "success",
                "chatgpt_oauth_access_token": "oauth-access-only",
                "chatgpt_refresh_token": "",
            },
        }
        with patch("webui.app.db.get_account", side_effect=lambda account_id: rows.get(int(account_id))):
            response = self.client.post(
                "/api/accounts/download-oauth-bulk",
                headers=self.headers,
                json={"account_ids": [12, 13], "prepare": True},
            )

        self.assertEqual(response.status_code, 200)
        prepared = response.get_json()
        self.assertEqual(prepared["added_count"], 1)
        self.assertEqual(prepared["error_count"], 1)
        download = self.client.get(prepared["download_url"], headers=self.headers)
        self.assertEqual(download.status_code, 200)
        # Chrome may retry the same download URL after the first response. The
        # prepared artifact must remain readable during its short TTL.
        download_retry = self.client.get(prepared["download_url"], headers=self.headers)
        self.assertEqual(download_retry.status_code, 200)
        self.assertEqual(download_retry.data, download.data)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            credential_names = [name for name in names if name != "manifest.json"]
            self.assertEqual(len(credential_names), 1)
            credential = json.loads(archive.read(credential_names[0]))
            self.assertEqual(credential["type"], "codex")
            self.assertEqual(credential["email"], "alias@example.com")
            self.assertEqual(credential["access_token"], "oauth-access")
            self.assertEqual(credential["refresh_token"], "oauth-refresh")
            self.assertEqual(credential["id_token"], "oauth-id")
            self.assertEqual(credential["account_id"], "account-12")
            self.assertEqual(credential["oauth_client_id"], "client-12")
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["source"], "local_chatgpt_oauth")
            self.assertEqual(len(manifest["errors"]), 1)

    def test_oauth_export_does_not_use_outlook_mailbox_refresh_token(self):
        rows = {
            14: {
                "id": 14,
                "email": "outlook@example.com",
                "email_source": "outlook",
                "access_token": "oauth-access",
                "refresh_token": "mailbox-refresh",
                "id_token": "oauth-id",
                "oauth_status": "success",
            },
        }
        with patch("webui.app.db.get_account", side_effect=lambda account_id: rows.get(int(account_id))):
            response = self.client.post(
                "/api/accounts/download-oauth-bulk",
                headers=self.headers,
                json={"account_ids": [14], "prepare": True},
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn("完整 OAuth", response.get_json()["error"])

    def test_account_toolbar_uses_oauth_export_label_instead_of_legacy_cpa_label(self):
        from pathlib import Path

        template = (Path(__file__).resolve().parent.parent / "webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="btnDownloadSelectedCpaV2"', template)
        self.assertIn("导出完整OAuth", template)
        self.assertNotIn('id="btnDownloadSelectedCpaV2" disabled title="从 CPA auth-files 下载选中账号的 Codex JSON，并打包 ZIP">下载CPA', template)

    def test_frontend_complete_oauth_check_requires_actual_credential_fields(self):
        from pathlib import Path

        template = (Path(__file__).resolve().parent.parent / "webui/templates/index.html").read_text(encoding="utf-8")
        start = template.index("function hasCompleteOAuthCredential")
        end = template.index("function accountSelectionQuery", start)
        helper = template[start:end]
        self.assertNotIn("oauth_status || '').toLowerCase() === 'success') return true", helper)
        self.assertIn("chatgpt_refresh_token", helper)
        self.assertIn("chatgpt_id_token", helper)

    def test_oauth_import_accepts_zip_and_delegates_complete_credentials(self):
        credential = {
            "type": "codex",
            "email": "alias@icloud.com",
            "access_token": "oauth-access",
            "refresh_token": "oauth-refresh",
            "id_token": "oauth-id",
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("codex-alias-oauth.json", json.dumps(credential))
        archive.seek(0)
        with patch("webui.app.db.import_chatgpt_oauth_credentials", return_value={
            "inserted": 0, "updated": 1, "skipped": 0, "errors": [], "items": [],
        }) as importer:
            response = self.client.post(
                "/api/accounts/import-oauth",
                headers=self.headers,
                data={"file": (archive, "accounts-oauth.zip")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["updated"], 1)
        importer.assert_called_once()
        self.assertEqual(importer.call_args.args[0][0]["refresh_token"], "oauth-refresh")

    def test_oauth_import_accepts_accounts_wrapper_and_access_only_record(self):
        payload = {
            "accounts": [
                {
                    "email": "access-only@icloud.com",
                    "access_token": "access-token",
                    "email_source": "icloud",
                },
            ],
        }
        with patch("webui.app.db.import_account_credentials", return_value={
            "inserted": 1, "updated": 0, "skipped": 0, "errors": [],
            "items": [], "oauth_status": {"access_only": 1},
        }) as importer:
            response = self.client.post(
                "/api/accounts/import-oauth",
                headers=self.headers,
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        importer.assert_called_once()
        records = importer.call_args.args[0]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "access-only@icloud.com")
        self.assertEqual(records[0]["access_token"], "access-token")

    def test_import_button_is_not_tied_to_account_selection(self):
        from pathlib import Path

        template = (Path(__file__).resolve().parent.parent / "webui/templates/index.html").read_text(encoding="utf-8")
        toolbar = template[template.index("id=\"btnImportOAuthV2\"") - 500:template.index("id=\"btnImportOAuthV2\"") + 500]
        self.assertNotIn("disabled", toolbar.split("id=\"btnImportOAuthV2\"")[0].split("<button")[-1])
        self.assertIn("function openOAuthImportPicker()", template)

    def test_frontend_exposes_filter_wide_selection_helpers(self):
        from pathlib import Path

        template = (Path(__file__).resolve().parent.parent / "webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("fetchAllAccountRowsForSelection", template)
        self.assertIn("fetchAllOutlookRowsForSelection", template)
        self.assertIn("fetchAllJobRowsForSelection", template)
        self.assertIn("fetchAllCodexRowsForSelection", template)

    def test_account_migration_export_includes_access_only_account(self):
        rows = {
            21: {"id": 21, "email": "access-only@icloud.com", "access_token": "access-token", "email_source": "icloud"},
        }
        with patch("webui.app.db.get_account", side_effect=lambda account_id: rows.get(int(account_id))):
            response = self.client.post(
                "/api/accounts/download-credentials-bulk",
                headers=self.headers,
                json={"account_ids": [21], "prepare": True},
            )
        self.assertEqual(response.status_code, 200)
        prepared = response.get_json()
        download = self.client.get(prepared["download_url"], headers=self.headers)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            files = [name for name in archive.namelist() if name != "manifest.json"]
            self.assertEqual(len(files), 1)
            payload = json.loads(archive.read(files[0]))
            self.assertEqual(payload["access_token"], "access-token")
            self.assertEqual(payload["credential_kind"], "access_only")


if __name__ == "__main__":
    unittest.main()
