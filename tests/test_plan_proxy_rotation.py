import unittest
from unittest.mock import Mock, call, patch

from core import chatgpt_plan


class PlanProxyRotationTests(unittest.TestCase):
    @staticmethod
    def _session_failure() -> Mock:
        env = Mock()
        env.session.get.side_effect = ConnectionError("CONNECT tunnel failed, response 503")
        return env

    def test_default_pool_route_rotates_on_retryable_failure(self):
        first = self._session_failure()
        second = self._session_failure()
        routes = [
            {"proxy": "http://IDENTITY_1@127.0.0.1:2260", "proxy_mode": "proxy", "network_route": "proxy"},
            {"proxy": "http://IDENTITY_2@127.0.0.1:2260", "proxy_mode": "proxy", "network_route": "proxy"},
        ]
        with patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=routes) as resolve, patch.object(
            chatgpt_plan, "BrowserSession", side_effect=[first, second]
        ) as browser:
            result = chatgpt_plan.check_account_plan(
                "e30.e30.signature",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(resolve.call_args_list, [call(None), call(None)])
        self.assertEqual(
            browser.call_args_list,
            [
                call(proxy="http://IDENTITY_1@127.0.0.1:2260", detect_exit_geo=False),
                call(proxy="http://IDENTITY_2@127.0.0.1:2260", detect_exit_geo=False),
            ],
        )

    def test_explicit_proxy_is_stable_across_retries(self):
        first = self._session_failure()
        second = self._session_failure()
        route = {"proxy": "http://EXPLICIT", "proxy_mode": "request", "network_route": "proxy"}
        with patch.object(chatgpt_plan, "resolve_plan_check_route", return_value=route) as resolve, patch.object(
            chatgpt_plan, "BrowserSession", side_effect=[first, second]
        ) as browser:
            result = chatgpt_plan.check_account_plan(
                "e30.e30.signature",
                proxy="http://EXPLICIT",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertFalse(result["ok"])
        resolve.assert_called_once_with("http://EXPLICIT")
        self.assertEqual(browser.call_count, 2)


if __name__ == "__main__":
    unittest.main()
