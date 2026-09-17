# -*- coding: utf-8 -*-
"""单文件 JSON 导出/导入往返：密码与 2FA 必须一起落库。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


def _db_storage_patches(root: Path) -> dict:
    return {
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_OUTLOOK_JSON": root / "outlook.json",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_JOBS_JSON": root / "jobs.json",
        "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
        "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
        "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
        "_LEGACY_SQLITE": root / "legacy.db",
        "_CODEX_DIR": root / "codex_accounts",
        "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
        "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
        "_SQLITE_READY": False,
        "_SQLITE_READY_PATH": None,
    }


class SingleJsonRoundTripTests(unittest.TestCase):
    def test_import_writes_password_and_totp_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                db.insert_account(email="rt@example.com", access_token="AT-1", plan_type="free")
                result = db.import_account_credentials([{
                    "type": "codex",
                    "email": "rt@example.com",
                    "password": "Pw-Secret-1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "access_token": "AT-2",
                    "refresh_token": "RT-2",
                    "id_token": "ID-2",
                }])
                acc = db.get_account_by_email("rt@example.com") or {}
                self.assertEqual(str(acc.get("password") or ""), "Pw-Secret-1")
                self.assertEqual(str(acc.get("totp_secret") or ""), "JBSWY3DPEHPK3PXP")
                self.assertGreaterEqual(int(result.get("password_imported") or 0), 1)
                self.assertGreaterEqual(int(result.get("twofa_imported") or 0), 1)

    def test_import_never_overwrites_existing_password(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                db.insert_account(email="keep@example.com", access_token="AT-1")
                db.update_account_registration_password("keep@example.com", "Local-Pw")
                db.import_account_credentials([{
                    "email": "keep@example.com",
                    "password": "Imported-Pw",
                    "access_token": "AT-2",
                }])
                acc = db.get_account_by_email("keep@example.com") or {}
                self.assertEqual(str(acc.get("password") or ""), "Local-Pw")


if __name__ == "__main__":
    unittest.main()
