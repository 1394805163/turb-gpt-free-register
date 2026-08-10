# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from config import proxy
from core import account_liveness, cloakbrowser_driver, live_check_service, resin_proxy_status
from webui import config_editor


class MihomoUsProxyTests(unittest.TestCase):
    @staticmethod
    def _response(payload, status=200):
        response = Mock()
        response.status_code = status
        response.json.return_value = payload
        response.raise_for_status.side_effect = None
        return response

    def test_selects_only_us_node_from_chatgpt_us_group(self):
        session = Mock()
        session.get.return_value = self._response({
            "name": "chatgpt us",
            "type": "Selector",
            "now": "DIRECT",
            "all": ["DIRECT", "日本-东京", "🇺🇸 US-Los-Angeles", "REJECT"],
        })
        session.put.return_value = self._response({})

        with patch.object(proxy.random, "choice", return_value="🇺🇸 US-Los-Angeles"):
            selected = proxy.select_mihomo_us_proxy(
                controller_url="http://127.0.0.1:9090",
                secret="controller-secret",
                group="chatgpt us",
                proxy_url="socks5h://127.0.0.1:7897",
                session=session,
            )

        self.assertEqual(selected["node_name"], "🇺🇸 US-Los-Angeles")
        self.assertEqual(selected["proxy_url"], "socks5h://127.0.0.1:7897")
        self.assertEqual(session.put.call_args.kwargs["json"], {"name": "🇺🇸 US-Los-Angeles"})
        self.assertEqual(
            session.get.call_args.kwargs["headers"]["Authorization"],
            "Bearer controller-secret",
        )

    def test_rotation_prefers_a_different_us_node_than_current(self):
        session = Mock()
        session.get.return_value = self._response({
            "name": "chatgpt us",
            "type": "Selector",
            "now": "🇺🇸 US-01",
            "all": ["🇺🇸 US-01", "🇺🇸 US-02", "日本-东京"],
        })
        session.put.return_value = self._response({})

        with patch.object(proxy.random, "choice", wraps=proxy.random.choice) as choose:
            selected = proxy.select_mihomo_us_proxy(
                controller_url="http://127.0.0.1:9090",
                secret="controller-secret",
                group="chatgpt us",
                proxy_url="socks5h://127.0.0.1:7897",
                allow_transparent=True,
                session=session,
            )

        self.assertEqual(selected["node_name"], "🇺🇸 US-02")
        self.assertEqual(choose.call_args.args[0], ["🇺🇸 US-02"])

    def test_no_us_node_or_controller_error_never_falls_back_to_direct(self):
        session = Mock()
        session.get.return_value = self._response({"all": ["DIRECT", "日本-东京", "REJECT"]})

        with self.assertRaisesRegex(RuntimeError, "美国节点"):
            proxy.select_mihomo_us_proxy(
                controller_url="http://127.0.0.1:9090",
                secret="",
                group="chatgpt us",
                proxy_url="socks5h://127.0.0.1:7897",
                session=session,
            )

        with patch.object(proxy, "select_mihomo_us_proxy", side_effect=ConnectionError("controller down")), patch.object(
            proxy, "REGISTRATION_PROXY_REQUIRED", False, create=True
        ), patch.object(proxy, "MIHOMO_US_FALLBACK_ENABLED", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "Mihomo"):
                proxy.pick_registration_proxy()

        with patch.object(proxy, "REGISTRATION_PROXY_REQUIRED", False), patch.object(
            proxy, "MIHOMO_US_FALLBACK_ENABLED", True
        ), patch.object(proxy, "MIHOMO_US_GROUP", ""):
            with self.assertRaisesRegex(RuntimeError, "配置不完整"):
                proxy.pick_registration_proxy()

    def test_pick_registration_proxy_accepts_configured_chatgpt_group_name(self):
        selected = {
            "mode": "mihomo_us",
            "group": "🤖 ChatGPT",
            "node_name": "🇺🇸 US-Los-Angeles",
            "proxy_url": "http://192.168.6.1:7890",
        }
        with patch.object(proxy, "REGISTRATION_PROXY_REQUIRED", False), patch.object(
            proxy, "MIHOMO_US_FALLBACK_ENABLED", True
        ), patch.object(proxy, "MIHOMO_US_GROUP", "🤖 ChatGPT"), patch.object(
            proxy, "select_mihomo_us_proxy", return_value=selected
        ) as select_proxy:
            self.assertEqual(proxy.pick_registration_proxy(), selected)

        self.assertEqual(select_proxy.call_args.kwargs["group"], "🤖 ChatGPT")

    def test_transparent_router_mode_needs_no_explicit_proxy_after_us_switch(self):
        session = Mock()
        session.get.return_value = self._response({
            "name": "🤖 ChatGPT",
            "type": "Selector",
            "now": "DIRECT",
            "all": ["DIRECT", "🇺🇸 美国节点", "🇯🇵 日本节点"],
        })
        session.put.return_value = self._response({})

        with patch.object(proxy.random, "choice", return_value="🇺🇸 美国节点"):
            selected = proxy.select_mihomo_us_proxy(
                controller_url="http://192.168.6.1:9090",
                secret="controller-secret",
                group="🤖 ChatGPT",
                proxy_url="socks5h://127.0.0.1:7897",
                allow_transparent=True,
                session=session,
            )

        self.assertEqual(selected["proxy_url"], "")
        self.assertTrue(selected["transparent"])
        self.assertEqual(selected["node_name"], "🇺🇸 美国节点")
        self.assertTrue(
            cloakbrowser_driver._allows_transparent_mihomo_route(selected)
        )

        with patch.object(proxy, "REGISTRATION_PROXY_REQUIRED", False), patch.object(
            proxy, "pick_registration_proxy", return_value=selected
        ):
            route = live_check_service._resolve_live_check_route(None)
        self.assertEqual(route["network_route"], "transparent")
        self.assertEqual(route["proxy_mode"], "mihomo_us_transparent")
        self.assertEqual(route["proxy"], "")

        with patch.object(proxy, "REGISTRATION_PROXY_REQUIRED", False), patch.object(
            proxy, "MIHOMO_US_FALLBACK_ENABLED", True
        ), patch.object(proxy, "MIHOMO_TRANSPARENT_ROUTING", True), patch.object(
            proxy, "MIHOMO_CONTROLLER_URL", "http://192.168.6.1:9090"
        ), patch.object(resin_proxy_status, "_tcp_reachable", return_value=True):
            status = resin_proxy_status.registration_proxy_status(check_tcp=True)
        self.assertTrue(status["ready"])
        self.assertEqual(status["mode"], "mihomo_us_transparent")

    def test_transparent_route_uses_openai_trace_for_geo_instead_of_direct_exit(self):
        with patch.object(
            cloakbrowser_driver,
            "_detect_openai_route_geo",
            return_value={"country": "US", "city": "LAX", "timezone": "America/Los_Angeles"},
        ) as route_geo, patch.object(
            cloakbrowser_driver,
            "_detect_cloak_exit_geo",
            side_effect=AssertionError("transparent route must not use a generic IP endpoint"),
        ):
            options = cloakbrowser_driver._build_cloak_locale_options(
                None,
                transparent_route=True,
            )

        route_geo.assert_called_once_with(None)
        self.assertEqual(options["geo"]["country"], "US")
        self.assertEqual(options["timezone"], "America/Los_Angeles")

    def test_mihomo_us_route_rejects_non_us_or_unverified_openai_exit(self):
        selection = {"mode": "mihomo_us", "transparent": True}

        with self.assertRaisesRegex(RuntimeError, "非美国"):
            cloakbrowser_driver._assert_mihomo_us_exit(selection, {"country": "HK"})
        with self.assertRaisesRegex(RuntimeError, "确认"):
            cloakbrowser_driver._assert_mihomo_us_exit(selection, {})

        cloakbrowser_driver._assert_mihomo_us_exit(selection, {"country": "US"})

    def test_transparent_liveness_preserves_explicit_empty_proxy(self):
        session = Mock()
        session.proxy = ""
        with patch.object(account_liveness, "BrowserSession", return_value=session) as browser_session, patch.object(
            account_liveness, "get_providers"
        ), patch.object(account_liveness, "get_csrf_token", return_value="csrf"), patch.object(
            account_liveness, "signin_openai", return_value="https://auth.openai.com/authorize"
        ):
            actual_session, authorize_url = account_liveness._network_preflight_with_retry(
                "alias@icloud.com",
                "",
                max_attempts=1,
            )

        self.assertIs(actual_session, session)
        self.assertEqual(authorize_url, "https://auth.openai.com/authorize")
        browser_session.assert_called_once_with(proxy="")

    def test_transparent_liveness_rotates_us_node_between_preflight_retries(self):
        first = Mock(proxy="")
        first.session.close.return_value = None
        second = Mock(proxy="")
        with patch.object(
            account_liveness,
            "BrowserSession",
            side_effect=[first, second],
        ) as browser_session, patch.object(
            account_liveness,
            "get_providers",
            side_effect=[ConnectionError("connection failed"), None],
        ), patch.object(
            account_liveness, "get_csrf_token", return_value="csrf"
        ), patch.object(
            account_liveness, "signin_openai", return_value="https://auth.openai.com/authorize"
        ), patch.object(
            proxy,
            "pick_registration_proxy",
            return_value={"mode": "mihomo_us", "transparent": True, "node_name": "US-02"},
        ) as rotate:
            actual_session, _ = account_liveness._network_preflight_with_retry(
                "alias@icloud.com",
                "",
                max_attempts=2,
                rotate_transparent_route=True,
            )

        self.assertIs(actual_session, second)
        self.assertEqual(browser_session.call_count, 2)
        self.assertEqual(browser_session.call_args_list[0].kwargs["proxy"], "")
        self.assertEqual(browser_session.call_args_list[1].kwargs["proxy"], "")
        rotate.assert_called_once_with()

    def test_live_check_service_enables_rotation_for_transparent_route(self):
        with patch.object(
            live_check_service.db, "mark_account_live_check_running", return_value=True
        ), patch.object(
            live_check_service,
            "_resolve_live_check_route",
            return_value={
                "proxy": "",
                "proxy_mode": "mihomo_us_transparent",
                "network_route": "transparent",
                "proxy_used": "router-policy",
            },
        ), patch.object(
            live_check_service,
            "check_account_liveness",
            return_value={"ok": False, "status": "temporary_error", "error": "test"},
        ) as check, patch.object(
            live_check_service.db, "update_account_liveness"
        ), patch.object(
            live_check_service, "_append_log"
        ):
            self.assertTrue(live_check_service._QUEUE_SLOTS.acquire(blocking=False))
            live_check_service._run_live_check_inner(
                account_id=1,
                email="alias@icloud.com",
                proxy=None,
                trigger="test",
            )

        check.assert_called_once_with(
            "alias@icloud.com",
            proxy="",
            clear_log=False,
            rotate_transparent_route=True,
        )

    def test_proxy_config_exposes_mihomo_us_fields(self):
        keys = {field["key"] for field in config_editor.EDITABLE_FIELDS}
        self.assertTrue({
            "MIHOMO_US_FALLBACK_ENABLED",
            "MIHOMO_CONTROLLER_URL",
            "MIHOMO_CONTROLLER_SECRET",
            "MIHOMO_US_GROUP",
            "MIHOMO_PROXY_URL",
            "MIHOMO_TRANSPARENT_ROUTING",
        }.issubset(keys))
        self.assertTrue(proxy.is_us_node_name("US Seattle 01"))
        self.assertFalse(proxy.is_us_node_name("DIRECT"))

    def test_resin_disabled_never_allows_explicit_or_liveness_direct_bypass(self):
        with patch.object(proxy, "REGISTRATION_PROXY_REQUIRED", False), patch.object(
            proxy, "MIHOMO_US_FALLBACK_ENABLED", False
        ), patch.object(
            proxy, "pick_registration_proxy", side_effect=RuntimeError("mihomo disabled")
        ) as pick_proxy, patch.object(
            cloakbrowser_driver._cfg, "CLOAK_USE_PROXY", True
        ):
            with self.assertRaisesRegex(RuntimeError, "mihomo disabled"):
                cloakbrowser_driver.build_cloak_driver(proxy="http://HOST:PORT")
            pick_proxy.assert_called_once_with()

        with patch.object(proxy, "REGISTRATION_PROXY_REQUIRED", False), patch.object(
            proxy, "MIHOMO_US_FALLBACK_ENABLED", False
        ), patch.object(
            proxy, "pick_registration_proxy", side_effect=RuntimeError("mihomo disabled")
        ) as pick_proxy, patch.object(
            live_check_service, "resolve_plan_check_route"
        ) as direct_route:
            with self.assertRaisesRegex(RuntimeError, "mihomo disabled"):
                live_check_service._resolve_live_check_route("http://HOST:PORT")
            pick_proxy.assert_called_once_with()
            direct_route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
