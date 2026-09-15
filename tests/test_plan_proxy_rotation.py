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

    def test_http_403_is_temporary_and_retries_with_a_new_route(self):
        first = Mock()
        first.session.get.return_value = Mock(status_code=403, text="Just a moment")
        second = Mock()
        second.session.get.return_value = Mock(status_code=403, text="Just a moment")
        routes = [
            {"proxy": "", "proxy_mode": "mihomo_excluded_transparent", "network_route": "transparent", "proxy_node": "JP04"},
            {"proxy": "", "proxy_mode": "mihomo_excluded_transparent", "network_route": "transparent", "proxy_node": "US05"},
        ]
        with patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=routes) as resolve, patch.object(
            chatgpt_plan, "BrowserSession", side_effect=[first, second]
        ):
            result = chatgpt_plan.check_account_plan(
                "e30.e30.signature",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(resolve.call_count, 2)

    def test_route_preflight_failure_rotates_before_openai_request(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        env = Mock()
        env.session.get.return_value = response
        second_route = {
            "proxy": "http://IDENTITY_2@127.0.0.1:2260",
            "proxy_mode": "mihomo",
            "network_route": "proxy",
            "proxy_node": "US02",
        }
        with patch.object(
            chatgpt_plan,
            "resolve_plan_check_route",
            side_effect=[ValueError("出口国家为 GB，不在允许地区内"), second_route],
        ) as resolve, patch.object(chatgpt_plan, "BrowserSession", return_value=env), patch.object(
            chatgpt_plan,
            "_probe_image_quota",
            return_value={
                "image_quota": 5,
                "image_quota_reset_at": "2026-09-10T00:00:00Z",
                "image_quota_unknown": False,
                "image_quota_checked_at": "2026-09-09T00:00:00Z",
                "image_quota_error": None,
                "image_quota_retryable": False,
            },
        ):
            result = chatgpt_plan.check_account_plan(
                "e30.e30.signature",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["image_quota"], 5)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(env.session.get.call_count, 1)

    def test_transient_image_quota_failure_rotates_and_rechecks(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        first = Mock()
        first.session.get.return_value = response
        second = Mock()
        second.session.get.return_value = response
        routes = [
            {"proxy": "", "proxy_mode": "mihomo", "network_route": "transparent", "proxy_node": "JP04"},
            {"proxy": "", "proxy_mode": "mihomo", "network_route": "transparent", "proxy_node": "US02"},
        ]
        quota_results = [
            {
                "image_quota": None,
                "image_quota_reset_at": None,
                "image_quota_unknown": True,
                "image_quota_checked_at": "2026-09-09T00:00:00Z",
                "image_quota_error": "HTTP 403",
                "image_quota_http_status": 403,
                "image_quota_retryable": True,
            },
            {
                "image_quota": 5,
                "image_quota_reset_at": "2026-09-10T00:00:00Z",
                "image_quota_unknown": False,
                "image_quota_checked_at": "2026-09-09T00:00:01Z",
                "image_quota_error": None,
                "image_quota_http_status": 200,
                "image_quota_retryable": False,
            },
        ]
        with patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=routes) as resolve, patch.object(
            chatgpt_plan, "BrowserSession", side_effect=[first, second]
        ), patch.object(chatgpt_plan, "_probe_image_quota", side_effect=quota_results):
            result = chatgpt_plan.check_account_plan(
                "e30.e30.signature",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["image_quota"], 5)
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(resolve.call_count, 2)

    def test_mihomo_plan_route_uses_registration_selector(self):
        selection = {
            "proxy_url": "",
            "transparent": True,
            "mode": "mihomo_excluded",
            "group": "🤖 ChatGPT",
            "node_name": "🇯🇵 JP04",
        }
        with patch("config.proxy.REGISTRATION_PROXY_SOURCE", "mihomo"), patch(
            "config.proxy.REGISTRATION_PROXY_REQUIRED", False
        ), patch("config.proxy.pick_registration_proxy", return_value=selection) as pick:
            route = chatgpt_plan.resolve_plan_check_route()

        self.assertEqual(route["proxy"], "")
        self.assertEqual(route["network_route"], "transparent")
        self.assertEqual(route["proxy_node"], "🇯🇵 JP04")
        pick.assert_called_once_with()

    def test_resin_plan_route_uses_registration_selector_and_preflight(self):
        selection = {
            "mode": "resin",
            "proxy_url": "http://RESIN_HOST:PORT",
            "preflight": {"ok": True, "country": "US"},
            "exit_country": "US",
        }
        with patch("config.proxy.REGISTRATION_PROXY_SOURCE", "resin"), patch(
            "config.proxy.REGISTRATION_PROXY_REQUIRED", True
        ), patch("config.proxy.pick_registration_proxy", return_value=selection) as pick:
            route = chatgpt_plan.resolve_plan_check_route()

        self.assertEqual(route["proxy"], "http://RESIN_HOST:PORT")
        self.assertEqual(route["network_route"], "proxy")
        self.assertEqual(route["proxy_mode"], "resin")
        pick.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
