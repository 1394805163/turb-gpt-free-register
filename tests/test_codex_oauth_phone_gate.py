# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import codex as codex_cfg
from core import roxy_codex_oauth


class CodexOAuthPhoneGateTests(unittest.TestCase):
    def test_disabled_phone_handling_keeps_legacy_continue_behavior(self):
        """禁用短信接码时，保持 v0.9.2 行为：不取号，把控制权交给后续授权流程。"""
        with (
            patch.object(codex_cfg, "CODEX_OAUTH_SKIP_PHONE_VERIFICATION", True),
            patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True),
        ):
            result = roxy_codex_oauth._do_phone_verification_if_present(object())
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
