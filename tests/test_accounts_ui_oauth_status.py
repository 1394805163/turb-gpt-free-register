# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class AccountsUiOAuthStatusTests(unittest.TestCase):
    def test_complete_oauth_status_overrides_stale_codex_status(self):
        """紧凑列表未下发凭据字段时，OAuth 成功必须仍显示已完成。"""
        template = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        start = template.index("function hasCompleteOAuthCredential(account) {")
        end = template.index("function accountSelectionQuery", start)
        function_body = template[start:end]

        expected = "if (String(account.oauth_status || '').trim().toLowerCase() === 'success') return true;"
        self.assertIn(expected, function_body)
        self.assertLess(function_body.index(expected), function_body.index("const source ="))


if __name__ == "__main__":
    unittest.main()
