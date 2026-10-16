# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.openai_auth as openai_auth


class _Session:
    def __init__(self):
        self.calls = 0
        self.reset_count = 0
        self.cookie_jar_identity = object()

    def get_auth_navigate_headers(self, **_kwargs):
        return {"sec-fetch-mode": "navigate"}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            error = RuntimeError("HTTP Error 403")
            error.response = SimpleNamespace(status_code=403)
            raise error
        return SimpleNamespace(
            status_code=200,
            url="https://auth.openai.com/email-verification",
            raise_for_status=lambda: None,
        )

    def reset_circuit_breaker(self):
        self.reset_count += 1


class AuthorizeRetryTests(unittest.TestCase):
    def test_403_retries_within_same_session_and_recovers(self):
        # 合并后语义：403 在同会话内重试（保留指纹/Cookie，不 replay OAuth state）。
        # 上游旧测试假设"403 立即停"，与其自身实现（同样加了 403 重试）相矛盾。
        session = _Session()
        cookie_jar_identity = session.cookie_jar_identity

        with patch.object(openai_auth, "_proxy_retry_config", return_value=(3, 0.0)), \
             patch.object(openai_auth.time, "sleep"):
            final_url = openai_auth.follow_authorize(
                session,
                "https://auth.openai.com/api/accounts/authorize?state=test",
            )

        # 第 1 次 403 → 重试第 2 次成功（_Session 的 get 第二次返回 200）。
        self.assertEqual(session.calls, 2)
        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertIs(session.cookie_jar_identity, cookie_jar_identity)


if __name__ == "__main__":
    unittest.main()
