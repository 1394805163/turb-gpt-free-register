# -*- coding: utf-8 -*-
"""账号启用 2FA 后的 mfa_challenge 登录支持。"""
import json
import unittest
from unittest.mock import patch

from core.openai_auth import validate_mfa_totp
from core import account_liveness


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self):
        self.headers_calls = []
        self.posts = []

    def get_auth_headers(self, referer=""):
        self.headers_calls.append(referer)
        return {}

    def post(self, url, headers=None, data=None):
        self.posts.append((url, json.loads(data)))
        return _Resp()


class MfaChallengeLoginTests(unittest.TestCase):
    def test_validate_mfa_totp_posts_type_code_id(self):
        session = _Session()
        cu = validate_mfa_totp(session, "123456", "factor-1", referer="https://auth.openai.com/mfa-challenge/factor-1")
        self.assertIn("callback", cu)
        url, body = session.posts[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/mfa/verify")
        self.assertEqual(body, {"type": "totp", "code": "123456", "id": "factor-1"})

    def test_pass_mfa_challenge_uses_stored_secret(self):
        seen = {}

        def fake_validate(session, code, factor_id, *, referer=""):
            seen["code"] = code
            seen["factor_id"] = factor_id
            seen["referer"] = referer
            return "https://chatgpt.com/api/auth/callback/openai?code=xyz"

        challenge = {
            "continue_url": "https://auth.openai.com/mfa-challenge/factor-1",
            "page": {"type": "mfa_challenge", "payload": {"factors": [{"id": "factor-1", "factor_type": "totp"}]}},
        }
        with patch.object(account_liveness.db, "get_account_by_email", return_value={"totp_secret": "JBSWY3DPEHPK3PXP"}), \
             patch("core.openai_auth.validate_mfa_totp", side_effect=fake_validate):
            out = account_liveness._pass_mfa_challenge(object(), "a@b.c", challenge)

        self.assertEqual(out["continue_url"], "https://chatgpt.com/api/auth/callback/openai?code=xyz")
        self.assertEqual(seen["factor_id"], "factor-1")
        self.assertEqual(len(str(seen["code"])), 6)
        self.assertTrue(str(seen["code"]).isdigit())
        self.assertIn("mfa-challenge", seen["referer"])

    def test_pass_mfa_challenge_without_secret_raises(self):
        challenge = {"continue_url": "u", "page": {"type": "mfa_challenge", "payload": {"factors": [{"id": "f"}]}}}
        with patch.object(account_liveness.db, "get_account_by_email", return_value={}):
            with self.assertRaises(RuntimeError):
                account_liveness._pass_mfa_challenge(object(), "a@b.c", challenge)

    def test_pass_mfa_challenge_without_factor_raises(self):
        challenge = {"continue_url": "u", "page": {"type": "mfa_challenge", "payload": {"factors": []}}}
        with patch.object(account_liveness.db, "get_account_by_email", return_value={"totp_secret": "JBSWY3DPEHPK3PXP"}):
            with self.assertRaises(RuntimeError):
                account_liveness._pass_mfa_challenge(object(), "a@b.c", challenge)


if __name__ == "__main__":
    unittest.main()
