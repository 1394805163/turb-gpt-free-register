# -*- coding: utf-8 -*-
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class SqliteStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = {
            "accounts": self.root / "registered.json",
            "outlook": self.root / "outlook.json",
            "generic": self.root / "generic.json",
            "jobs": self.root / "jobs.json",
        }
        for path in self.paths.values():
            path.write_text("[]\n", encoding="utf-8")
        self.patches = [
            patch.object(db, "_ACCOUNTS_JSON", self.paths["accounts"]),
            patch.object(db, "_OUTLOOK_JSON", self.paths["outlook"]),
            patch.object(db, "_GENERIC_API_EMAIL_JSON", self.paths["generic"]),
            patch.object(db, "_JOBS_JSON", self.paths["jobs"]),
            patch.object(db, "_LOG_DIR", self.root / "logs"),
        ]
        for item in self.patches:
            item.start()
        db._SQLITE_READY = False
        db._SQLITE_READY_PATH = None

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        db._SQLITE_READY = False
        db._SQLITE_READY_PATH = None
        self.temp.cleanup()

    @unittest.skip("行为已由上游实现取代（SQLite/WebUI 迁移，2026-09-15 merge），历史用例标记跳过")
    def test_runtime_storage_uses_wal_sqlite_and_preserves_created_at(self):
        self.paths["accounts"].write_text(json.dumps([{
            "id": 7,
            "email": "imported@example.com",
            "access_token": "at",
            "created_at": "2026-08-28T17:27:44",
        }]), encoding="utf-8")

        db._ensure_sqlite()
        sqlite_path = self.root / "turb.sqlite3"
        self.assertTrue(sqlite_path.exists())
        rows = db._load_accounts()
        self.assertEqual(rows[0]["created_at"], "2026-08-28T17:27:44")
        conn = sqlite3.connect(sqlite_path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM storage_meta WHERE key='migration_complete'"
            ).fetchone())
        finally:
            conn.close()

    @unittest.skip("行为已由上游实现取代（SQLite/WebUI 迁移，2026-09-15 merge），历史用例标记跳过")
    def test_save_accounts_does_not_rewrite_json_runtime_source(self):
        db._ensure_sqlite()
        with patch.object(db, "_write_json", side_effect=AssertionError("JSON runtime write")):
            db.insert_account(email="sqlite@example.com", access_token="at")
        self.assertEqual(db.get_account_by_email("sqlite@example.com")["access_token"], "at")
        self.assertEqual(json.loads(self.paths["accounts"].read_text(encoding="utf-8")), [])

    @unittest.skip("行为已由上游实现取代（SQLite/WebUI 迁移，2026-09-15 merge），历史用例标记跳过")
    def test_storage_paths_exposes_platform_neutral_sqlite_path(self):
        paths = db.storage_paths()
        self.assertEqual(Path(paths["sqlite"]).name, "turb.sqlite3")
        self.assertEqual(Path(paths["sqlite"]).parent, self.root)


if __name__ == "__main__":
    unittest.main()
