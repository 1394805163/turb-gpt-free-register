# -*- coding: utf-8 -*-
"""账号级画像种子：优先读账号记录 cloak_profile_seed，没有记录回退 sha256(email)。"""
import hashlib
import unittest
from unittest.mock import patch

from core import cloakbrowser_driver as driver_mod


class AccountFingerprintSeedTests(unittest.TestCase):
    def test_falls_back_to_sha256_without_record(self):
        with patch("core.db.get_account_by_email", return_value=None):
            seed = driver_mod.account_fingerprint_seed("fixture@example.com")
        expected = hashlib.sha256(b"cloak-profile:fixture@example.com").hexdigest()[:16]
        self.assertEqual(seed, expected)

    def test_prefers_recorded_seed(self):
        with patch("core.db.get_account_by_email", return_value={"cloak_profile_seed": "abcdef1234567890"}):
            seed = driver_mod.account_fingerprint_seed("fixture@example.com")
        self.assertEqual(seed, "abcdef1234567890")

    def test_db_error_falls_back(self):
        with patch("core.db.get_account_by_email", side_effect=RuntimeError("boom")):
            seed = driver_mod.account_fingerprint_seed("fixture@example.com")
        expected = hashlib.sha256(b"cloak-profile:fixture@example.com").hexdigest()[:16]
        self.assertEqual(seed, expected)

    def test_empty_email_returns_empty(self):
        self.assertEqual(driver_mod.account_fingerprint_seed(""), "")


if __name__ == "__main__":
    unittest.main()
