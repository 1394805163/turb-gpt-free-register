# -*- coding: utf-8 -*-
"""已托管下游/已导出的账号，本地 RT 刷新必须被拒绝（不抢下游的一次性 RT）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.oauth_refresh import refresh_account_credentials


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


def _seed_account(email: str, **extra) -> None:
    db.insert_account(
        email=email,
        access_token="at-old",
        chatgpt_oauth={
            "access_token": "at-old",
            "refresh_token": "rt-old",
            "id_token": "id-old",
            "oauth_client_id": "cid-test",
        },
        email_source="test",
    )
    if extra:
        rows = db._load_accounts()
        for row in rows:
            if (row.get("email") or "").lower() == email.lower():
                row.update(extra)
        db._save_accounts(rows)


class RtRefreshGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = patch.multiple(db, **_db_storage_patches(Path(self.tmp.name)))
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def test_exported_account_refresh_refused_without_network(self):
        _seed_account("exported@example.com", exported_at="2026-09-19T00:00:00")
        with patch("curl_cffi.requests.Session") as sess:
            res = refresh_account_credentials("exported@example.com")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("status"), "downstream_managed")
        sess.assert_not_called()

    def test_pushed_account_refresh_refused_without_network(self):
        _seed_account("pushed@example.com", push_status="pushed")
        with patch("curl_cffi.requests.Session") as sess:
            res = refresh_account_credentials("pushed@example.com")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("status"), "downstream_managed")
        sess.assert_not_called()

    def test_force_refresh_bypasses_guard(self):
        _seed_account("force@example.com", exported_at="2026-09-19T00:00:00")
        with patch("curl_cffi.requests.Session") as sess:
            sess.return_value.post.side_effect = RuntimeError("network-blocked-in-test")
            res = refresh_account_credentials("force@example.com", force=True)
        self.assertNotEqual(res.get("status"), "downstream_managed")
        self.assertEqual(sess.call_count, 1)

    def test_plain_account_not_blocked(self):
        _seed_account("plain@example.com")
        with patch("curl_cffi.requests.Session") as sess:
            sess.return_value.post.side_effect = RuntimeError("network-blocked-in-test")
            res = refresh_account_credentials("plain@example.com")
        self.assertNotEqual(res.get("status"), "downstream_managed")
        self.assertEqual(sess.call_count, 1)


if __name__ == "__main__":
    unittest.main()
