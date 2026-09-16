# -*- coding: utf-8 -*-
"""邮箱池导入 overwrite（覆盖同名）语义测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from core import db, icloud_mail_client


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


class DbPoolOverwriteTests(unittest.TestCase):
    def test_generic_overwrite_updates_code_url_and_revives_failed(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                self.assertEqual(
                    db.import_generic_api_emails([
                        {"email": "g@example.com", "code_url": "https://mail.example/v1"},
                    ]),
                    (1, 0),
                )
                db.release_generic_api_email("g@example.com", status="failed", note="临时失败")
                # 不覆盖：跳过且不改动
                self.assertEqual(
                    db.import_generic_api_emails([
                        {"email": "g@example.com", "code_url": "https://mail.example/v2"},
                    ]),
                    (0, 1),
                )
                row = db._find_by_email(db._load_generic_api_emails(), "g@example.com")
                self.assertEqual(row["code_url"], "https://mail.example/v1")
                self.assertEqual(row["status"], "failed")
                # 覆盖：更新取码地址 + failed → available
                self.assertEqual(
                    db.import_generic_api_emails([
                        {"email": "g@example.com", "code_url": "https://mail.example/v2"},
                    ], overwrite=True),
                    (1, 0),
                )
                row = db._find_by_email(db._load_generic_api_emails(), "g@example.com")
                self.assertEqual(row["code_url"], "https://mail.example/v2")
                self.assertEqual(row["status"], "available")

    def test_generic_overwrite_keeps_used_status(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                db.import_generic_api_emails([
                    {"email": "used@example.com", "code_url": "https://mail.example/a"},
                ])
                db.release_generic_api_email("used@example.com", status="used")
                self.assertEqual(
                    db.import_generic_api_emails([
                        {"email": "used@example.com", "code_url": "https://mail.example/b"},
                    ], overwrite=True),
                    (1, 0),
                )
                row = db._find_by_email(db._load_generic_api_emails(), "used@example.com")
                self.assertEqual(row["code_url"], "https://mail.example/b")
                self.assertEqual(row["status"], "used")

    def test_outlook_overwrite_updates_refresh_token(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                self.assertEqual(
                    db.import_outlook_accounts([
                        {"email": "o@example.com", "password": "pw1", "client_id": "cid1", "refresh_token": "rt1"},
                    ]),
                    (1, 0),
                )
                self.assertEqual(
                    db.import_outlook_accounts([
                        {"email": "o@example.com", "password": "pw2", "client_id": "cid2", "refresh_token": "rt2"},
                    ]),
                    (0, 1),
                )
                row = db._find_by_email(db._load_outlook(), "o@example.com")
                self.assertEqual(row["refresh_token"], "rt1")
                self.assertEqual(
                    db.import_outlook_accounts([
                        {"email": "o@example.com", "password": "pw2", "client_id": "cid2", "refresh_token": "rt2"},
                    ], overwrite=True),
                    (1, 0),
                )
                row = db._find_by_email(db._load_outlook(), "o@example.com")
                self.assertEqual(row["refresh_token"], "rt2")
                self.assertEqual(row["password"], "pw2")

    def test_imap_overwrite_updates_password(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                rec1 = {"email": "i@example.com", "imap_password": "p1", "imap_server": "imap.example.com", "imap_port": 993, "imap_ssl": True}
                rec2 = {"email": "i@example.com", "imap_password": "p2", "imap_server": "imap.example.com", "imap_port": 993, "imap_ssl": True}
                self.assertEqual(db.import_imap_emails([rec1]), (1, 0))
                self.assertEqual(db.import_imap_emails([rec2]), (0, 1))
                self.assertEqual(db._find_by_email(db._load_imap_emails(), "i@example.com")["imap_password"], "p1")
                self.assertEqual(db.import_imap_emails([rec2], overwrite=True), (1, 0))
                self.assertEqual(db._find_by_email(db._load_imap_emails(), "i@example.com")["imap_password"], "p2")


class ICloudOverwriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mailbox_file = root / "icloud_mailboxes.txt"
        self.state_file = root / "icloud_mailboxes.json"
        self.path_patch = patch.object(email_config, "ICLOUD_MAILBOXES_FILE", str(self.mailbox_file))
        self.state_patch = patch.object(icloud_mail_client, "_STATE_FILE", self.state_file)
        self.path_patch.start()
        self.state_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.state_patch.stop)

    def test_overwrite_updates_label_and_revives_failed(self):
        first = icloud_mail_client.import_mailboxes("a@icloud.com----v1")
        self.assertEqual((first["inserted"], first["updated"], first["skipped"]), (1, 0, 0))
        icloud_mail_client.set_mailbox_status("a@icloud.com", "failed", "临时失败")
        # 不覆盖：跳过，标签与状态保持
        again = icloud_mail_client.import_mailboxes("a@icloud.com----v2")
        self.assertEqual((again["inserted"], again["updated"], again["skipped"]), (0, 0, 1))
        row = icloud_mail_client.list_mailboxes()[0]
        self.assertEqual((row["label"], row["status"]), ("v1", "failed"))
        # 覆盖：更新标签 + failed → available
        forced = icloud_mail_client.import_mailboxes("a@icloud.com----v2", overwrite=True)
        self.assertEqual((forced["inserted"], forced["updated"], forced["skipped"]), (0, 1, 0))
        row = icloud_mail_client.list_mailboxes()[0]
        self.assertEqual((row["label"], row["status"]), ("v2", "available"))

    def test_overwrite_keeps_used_status(self):
        icloud_mail_client.import_mailboxes("b@icloud.com----tag")
        icloud_mail_client.set_mailbox_status("b@icloud.com", "used")
        icloud_mail_client.import_mailboxes("b@icloud.com----tag2", overwrite=True)
        row = icloud_mail_client.list_mailboxes()[0]
        self.assertEqual(row["label"], "tag2")
        self.assertEqual(row["status"], "used")


if __name__ == "__main__":
    unittest.main()
