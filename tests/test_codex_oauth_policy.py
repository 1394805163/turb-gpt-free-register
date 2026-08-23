# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core.codex_oauth_policy import evaluate_oauth_eligibility


class CodexOAuthPolicyTests(unittest.TestCase):
    NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    def account(self, **overrides):
        row = {
            "email": "account@example.com",
            "created_at": "2026-08-12T12:00:00+00:00",
            "token_expires_at": "2026-08-21T12:00:00+00:00",
            "token_expired": True,
            "access_token": "access-token",
            "chatgpt_refresh_token": "",
        }
        row.update(overrides)
        return row

    def test_old_account_with_expired_access_token_is_oauth_eligible(self):
        result = evaluate_oauth_eligibility(self.account(), now=self.NOW)

        self.assertTrue(result["eligible"])
        self.assertEqual(result["action"], "oauth")
        self.assertEqual(result["reason_code"], "eligible")

    def test_account_younger_than_min_age_is_routed_to_simple_check(self):
        result = evaluate_oauth_eligibility(
            self.account(created_at="2026-08-22T12:00:00+00:00"),
            now=self.NOW,
        )

        self.assertFalse(result["eligible"])
        self.assertEqual(result["action"], "plan_check")
        self.assertEqual(result["reason_code"], "account_too_new")

    def test_unexpired_access_token_is_oauth_eligible_when_account_is_old_enough(self):
        result = evaluate_oauth_eligibility(
            self.account(
                token_expires_at="2026-08-30T12:00:00+00:00",
                token_expired=False,
            ),
            now=self.NOW,
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["action"], "oauth")
        self.assertEqual(result["reason_code"], "eligible")

    def test_expiry_gate_is_disabled_by_default(self):
        from config import codex

        self.assertFalse(codex.CODEX_OAUTH_REQUIRE_EXPIRED_TOKEN)

    def test_existing_chatgpt_refresh_token_is_not_reauthorized(self):
        result = evaluate_oauth_eligibility(
            self.account(chatgpt_refresh_token="already-persisted"),
            now=self.NOW,
        )

        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason_code"], "oauth_already_persisted")

    def test_settings_are_read_from_codex_config(self):
        with patch("config.codex.CODEX_OAUTH_MIN_AGE_DAYS", 14), patch(
            "config.codex.CODEX_OAUTH_REQUIRE_EXPIRED_TOKEN", False
        ):
            result = evaluate_oauth_eligibility(
                self.account(created_at="2026-08-12T12:00:00+00:00"),
                now=self.NOW,
            )

        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason_code"], "account_too_new")


if __name__ == "__main__":
    unittest.main()
