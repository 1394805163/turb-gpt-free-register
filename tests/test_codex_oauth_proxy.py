# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import codex_retry_service, roxybrowser_client


class CodexOAuthProxyTests(unittest.TestCase):
    def test_codex_retry_passes_mihomo_proxy_to_oauth_driver(self):
        result = {"ok": True, "status": "success", "message": "ok"}
        with TemporaryDirectory() as tmpdir, patch("config.reload_all"), patch(
            "config.proxy.pick_registration_proxy",
            return_value={"proxy_url": "socks5h://127.0.0.1:7897", "mode": "mihomo_excluded"},
        ), patch(
            "core.registration_preflight.preflight_proxy",
            return_value={"ok": True, "country": "US", "ip": "198.51.100.10"},
        ), patch("core.codex_oauth.run_codex_oauth", return_value=result) as run_oauth, patch.object(
            codex_retry_service, "_persist_oauth_result_and_queue_liveness", return_value=result
        ), patch.object(
            codex_retry_service.db, "update_account_codex_status"
        ):
            actual = codex_retry_service.run_worker(
                "account@example.com",
                target_log_path=Path(tmpdir) / "retry.log",
            )

        self.assertTrue(actual["ok"])
        self.assertEqual(run_oauth.call_args.kwargs["proxy"], "socks5h://127.0.0.1:7897")

    def test_roxy_profile_uses_explicit_oauth_proxy_before_proxy_pool(self):
        client = roxybrowser_client.RoxyBrowserClient(proxy="socks5h://127.0.0.1:7897")
        with patch.object(roxybrowser_client._cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {"workspaceId": "w"}), patch.object(
            roxybrowser_client._cfg, "ROXY_CREATE_USE_PROXY_POOL", True
        ), patch.object(roxybrowser_client._cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), patch.object(
            roxybrowser_client._cfg, "ROXY_RANDOM_OS_ON_CREATE", False
        ), patch.object(roxybrowser_client, "_proxy_url_to_roxy_info", return_value={"host": "127.0.0.1", "port": 7897}) as to_info, patch.object(
            client, "request", return_value={"id": "profile-1"}
        ) as request:
            profile_id = client.create_profile()

        self.assertEqual(profile_id, "profile-1")
        to_info.assert_called_once_with("socks5h://127.0.0.1:7897")
        self.assertEqual(request.call_args.kwargs["json_body"]["proxyInfo"], {"host": "127.0.0.1", "port": 7897})

    def test_codex_retry_rotates_proxy_after_cloudflare_interception(self):
        first = {"ok": False, "status": "failed", "message": "ChatGPT/Cloudflare 拦截当前代理，准备轮换"}
        second = {"ok": True, "status": "success", "message": "ok"}
        selections = [
            {"proxy_url": "", "transparent": True, "mode": "mihomo_excluded", "node_name": "🇯🇵 JP04"},
            {"proxy_url": "", "transparent": True, "mode": "mihomo_excluded", "node_name": "🇺🇸 US05"},
        ]
        with TemporaryDirectory() as tmpdir, patch("config.reload_all"), patch.object(
            codex_retry_service, "_oauth_proxy_rotation_attempts", return_value=2
        ), patch.object(
            codex_retry_service, "_prepare_oauth_route", side_effect=lambda selection: selection
        ), patch(
            "config.proxy.pick_registration_proxy", side_effect=selections
        ), patch("core.codex_oauth.run_codex_oauth", side_effect=[first, second]) as run_oauth, patch.object(
            codex_retry_service, "_persist_oauth_result_and_queue_liveness", return_value=second
        ), patch.object(codex_retry_service.db, "update_account_codex_status"):
            actual = codex_retry_service.run_worker(
                "account@example.com",
                target_log_path=Path(tmpdir) / "retry.log",
            )

        self.assertTrue(actual["ok"])
        self.assertEqual(run_oauth.call_count, 2)
        self.assertEqual(
            [call.kwargs["proxy_selection"]["node_name"] for call in run_oauth.call_args_list],
            ["🇯🇵 JP04", "🇺🇸 US05"],
        )

    def test_codex_proxy_classifier_recognizes_http_forbidden_interception(self):
        self.assertTrue(
            codex_retry_service._is_oauth_proxy_rejection({
                "status": "failed",
                "message": "HTTP 403 Forbidden: challenge required",
            })
        )

    def test_codex_retry_rotates_when_proxy_selection_itself_fails(self):
        second_route = {
            "proxy_url": "http://RESIN_HOST:PORT",
            "transparent": False,
            "mode": "resin",
            "node_name": "RESIN-02",
        }
        success = {"ok": True, "status": "success", "message": "ok"}
        with TemporaryDirectory() as tmpdir, patch("config.reload_all"), patch.object(
            codex_retry_service, "_oauth_proxy_rotation_attempts", return_value=2
        ), patch.object(
            codex_retry_service,
            "_select_oauth_route",
            side_effect=[RuntimeError("代理预检失败: HTTP 403"), second_route],
        ) as select_route, patch(
            "core.codex_oauth.run_codex_oauth", return_value=success
        ) as run_oauth, patch.object(
            codex_retry_service, "_persist_oauth_result_and_queue_liveness", return_value=success
        ), patch.object(codex_retry_service.db, "update_account_codex_status"):
            actual = codex_retry_service.run_worker(
                "account@example.com",
                target_log_path=Path(tmpdir) / "retry.log",
            )

        self.assertTrue(actual["ok"])
        self.assertEqual(select_route.call_count, 2)
        self.assertEqual(run_oauth.call_count, 1)


if __name__ == "__main__":
    unittest.main()
