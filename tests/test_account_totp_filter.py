# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountTotpFilterTests(unittest.TestCase):
    def test_account_page_filters_enabled_disabled_pending_and_failed_totp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text(json.dumps([
                {
                    "id": 1,
                    "email": "enabled@example.com",
                    "access_token": "token-1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "totp_setup_status": "success",
                },
                {
                    "id": 2,
                    "email": "disabled@example.com",
                    "access_token": "token-2",
                },
                {
                    "id": 3,
                    "email": "queued@example.com",
                    "access_token": "token-3",
                    "totp_setup_status": "queued",
                },
                {
                    "id": 4,
                    "email": "running@example.com",
                    "access_token": "token-4",
                    "totp_setup_status": "running",
                },
                {
                    "id": 5,
                    "email": "failed@example.com",
                    "access_token": "token-5",
                    "totp_setup_status": "failed",
                },
            ], ensure_ascii=False), encoding="utf-8")

            missing = root / "missing.json"
            with patch.multiple(
                db,
                _ACCOUNTS_JSON=accounts_path,
                _LEGACY_ACCOUNTS_JSON=missing,
                _OUTLOOK_JSON=missing,
                _GENERIC_API_EMAIL_JSON=missing,
                _JOBS_JSON=missing,
                _DOMAIN_EMAIL_JSON=missing,
            ):
                def account_ids(totp_filter):
                    result = db.list_accounts_page(limit=20, totp_filter=totp_filter)
                    return [item["id"] for item in result["items"]]

                self.assertEqual(account_ids("enabled"), [1])
                self.assertEqual(account_ids("disabled"), [5, 4, 3, 2])
                self.assertEqual(account_ids("pending"), [4, 3])
                self.assertEqual(account_ids("failed"), [5])
                self.assertEqual(account_ids(""), [5, 4, 3, 2, 1])

                snapshot = db.list_account_plan_check_statuses(
                    limit=20,
                    totp_filter="enabled",
                )
                self.assertEqual([item["id"] for item in snapshot["items"]], [1])
                self.assertTrue(snapshot["items"][0]["totp_enabled"])

    def test_account_templates_expose_the_same_totp_filter_contract(self):
        root = Path(__file__).resolve().parents[1]
        modern = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        legacy = (root / "webui" / "templates" / "index_legacy.html").read_text(encoding="utf-8")
        for template, element_id in ((modern, "totpStatusFilterV2"), (legacy, "totpStatusFilter")):
            self.assertIn(element_id, template)
            self.assertIn("totp_status", template)
            self.assertIn("value=\"enabled\"", template)
            self.assertIn("value=\"disabled\"", template)


if __name__ == "__main__":
    unittest.main()
