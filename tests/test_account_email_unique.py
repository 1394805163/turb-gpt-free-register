# -*- coding: utf-8 -*-
"""邮箱是账号唯一键：DB 级唯一索引兜底。"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core import db
from tests.test_import_overwrite import _db_storage_patches


class AccountEmailUniqueTests(unittest.TestCase):
    def test_unique_index_blocks_duplicate_email(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                db.insert_account(email="uniq@example.com", access_token="AT-1")
                with closing(db._sqlite_conn()) as conn:
                    unique = [r["name"] for r in conn.execute("PRAGMA index_list(accounts)") if r["unique"]]
                    self.assertIn("idx_accounts_email_unique", unique)
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO accounts (email, payload) VALUES ('UNIQ@example.com', '{}')"
                        )

    def test_upsert_keeps_single_row(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **_db_storage_patches(Path(td))):
                first = db.insert_account(email="one@example.com", access_token="AT-1")
                second = db.insert_account(email="ONE@example.com", access_token="AT-2")
                self.assertEqual(first, second)
                with closing(db._sqlite_conn()) as conn:
                    count = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
                self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
