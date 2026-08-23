# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import roxy_codex_oauth


class CodexBrowserLoginStateTests(unittest.TestCase):
    def test_authorize_page_without_email_input_is_reported_instead_of_waiting_callback(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/api/accounts/authorize?client_id=fixture"
        with patch.object(roxy_codex_oauth, "_type_email_address", side_effect=RuntimeError("邮箱输入框不存在")), patch.object(
            roxy_codex_oauth, "_maybe_accept"
        ), patch.object(roxy_codex_oauth, "human_delay"), patch.object(
            roxy_codex_oauth, "_email_otp_page_state",
            return_value={"url": driver.current_url, "inputs": [], "buttons": [], "text": "Just a moment..."},
        ):
            with self.assertRaisesRegex(RuntimeError, "未找到邮箱输入框"):
                roxy_codex_oauth._fill_email_and_otp(
                    driver,
                    "account@example.com",
                    lambda *_args, **_kwargs: "123456",
                    driver.current_url,
                )


if __name__ == "__main__":
    unittest.main()
