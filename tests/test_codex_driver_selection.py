# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import codex as codex_config
from core import codex_oauth


class CodexDriverSelectionTests(unittest.TestCase):
    def test_cloak_driver_uses_cloak_builder_and_never_creates_roxy_profile(self):
        opened = object()
        driver = object()
        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "cloak"), patch(
            "core.cloakbrowser_driver.build_cloak_driver", return_value=(driver, opened)
        ) as build_cloak, patch(
            "core.roxybrowser_client.RoxyBrowserClient"
        ) as roxy_client, patch(
            "core.roxy_codex_oauth.run_roxy_codex_oauth",
            return_value={"ok": True, "status": "success"},
        ) as shared_browser_flow:
            result = codex_oauth.run_codex_oauth("account@example.com", force=True, proxy="http://HOST:PORT")

        self.assertTrue(result["ok"])
        build_cloak.assert_called_once_with(proxy="http://HOST:PORT", proxy_selection=None)
        roxy_client.assert_not_called()
        self.assertEqual(shared_browser_flow.call_args.kwargs["existing_driver"], driver)
        self.assertEqual(shared_browser_flow.call_args.kwargs["existing_opened"], opened)
        self.assertTrue(shared_browser_flow.call_args.kwargs["reuse_existing_profile"])


if __name__ == "__main__":
    unittest.main()
