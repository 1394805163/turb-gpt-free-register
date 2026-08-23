# -*- coding: utf-8 -*-
import tempfile
import unittest
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.image_quota import extract_image_quota


class ImageQuotaTests(unittest.TestCase):
    def test_extracts_image_gen_remaining_and_reset_time(self):
        result = extract_image_quota({
            "limits_progress": [
                {"feature_name": "image_gen", "remaining": 5, "reset_after": "2026-08-24T00:00:00Z"},
            ]
        })
        self.assertEqual(result["image_quota"], 5)
        self.assertEqual(result["image_quota_reset_at"], "2026-08-24T00:00:00Z")
        self.assertFalse(result["image_quota_unknown"])

    def test_missing_image_gen_limit_is_explicitly_unknown(self):
        result = extract_image_quota({"limits_progress": []})
        self.assertIsNone(result["image_quota"])
        self.assertTrue(result["image_quota_unknown"])

    def test_plan_check_persists_image_quota_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
                }.items():
                    stack.enter_context(patch.object(db, name, value))
                stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
                account_id = db.insert_account(email="quota@example.com", access_token="access-token")
                self.assertTrue(db.update_account_plan_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "checked_at": "2026-08-23T00:00:00",
                        "current_plan_type": "free",
                        "image_quota": 5,
                        "image_quota_reset_at": "2026-08-24T00:00:00Z",
                        "image_quota_unknown": False,
                        "image_quota_checked_at": "2026-08-23T00:00:00",
                    },
                ))
                stored = db.get_account(account_id)
                self.assertEqual(stored["image_quota"], 5)
                self.assertEqual(stored["image_quota_reset_at"], "2026-08-24T00:00:00Z")
                self.assertFalse(stored["image_quota_unknown"])
                self.assertIn("生图额度: 5", stored["note"])
                self.assertIn("重置: 2026-08-24T00:00:00Z", stored["note"])

    def test_account_view_reconstructs_image_quota_note_for_legacy_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts = root / "accounts.json"
            accounts.write_text(json.dumps([{
                "id": 1,
                "email": "legacy@example.com",
                "access_token": "access-token",
                "note": "",
                "image_quota": 5,
                "image_quota_reset_at": "2026-08-24T00:00:00Z",
                "image_quota_unknown": False,
            }]), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts), patch.object(
                db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"
            ):
                row = db.get_account(1)
            self.assertIn("生图额度: 5", row["note"])


if __name__ == "__main__":
    unittest.main()
