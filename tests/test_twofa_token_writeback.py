# -*- coding: utf-8 -*-
"""回归：setup_2fa 必须把重认证新 AT 交给调用方（token_out）。"""
import unittest
from unittest.mock import patch
import core.account_export as ae


class _S:
    def __init__(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""

    def reset_circuit_breaker(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""


def _run(out=None):
    with patch.object(ae, "_trigger_reauth_with_retry", return_value="https://auth/authorize"), \
         patch.object(ae, "_follow_reauth_with_retry"), \
         patch("core.openai_auth.send_email_otp"), \
         patch.object(ae, "_validate_reauth_otp", return_value="https://cb"), \
         patch.object(ae, "_exchange_new_token", return_value="FRESH_AT"), \
         patch.object(ae, "_enroll_totp", return_value=("SECRET", "sid")), \
         patch.object(ae, "_activate_totp"), \
         patch.object(ae, "human_delay"):
        return ae.setup_2fa(_S(), "u@example.com", otp_code="123456", token_out=out)


class TokenOutTests(unittest.TestCase):
    def test_setup_2fa_returns_fresh_token_via_token_out(self):
        out = {}
        self.assertEqual(_run(out), "SECRET")
        self.assertEqual(out.get("access_token"), "FRESH_AT")

    def test_token_out_optional(self):
        self.assertEqual(_run(), "SECRET")


if __name__ == "__main__":
    unittest.main()
