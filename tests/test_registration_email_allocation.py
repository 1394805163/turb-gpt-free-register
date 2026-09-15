# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from config import register as register_config
from core import browser_use_registration as browser_use
from core import email_provider
from core import registration_service
from core import roxy_registration as roxy


class DelayedEmailAllocationTests(unittest.TestCase):
    @patch("core.email_provider.acquire_email")
    def test_acquire_email_after_input_keeps_fixed_email_without_allocating(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            self.assertEqual(
                email_provider.acquire_email_after_input("fixed@example.com"),
                "fixed@example.com",
            )
        acquire.assert_not_called()

    @patch("core.email_provider.acquire_email", return_value="allocated@example.com")
    def test_acquire_email_after_input_allocates_only_for_automatic_mode(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.acquire_email_after_input(None),
                "allocated@example.com",
            )
        acquire.assert_called_once_with()

    @patch("core.email_provider.acquire_email")
    def test_registration_preparation_does_not_allocate_automatic_email(self, acquire):
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", ""
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True), patch(
            "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
        ):
            email, name, birthday = registration_service._prepare_registration_args()

        self.assertEqual(email, "")
        self.assertTrue(name)
        self.assertEqual(birthday, "1990-01-01")
        acquire.assert_not_called()

    def test_roxy_finds_input_before_allocating_email(self):
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "roxy@example.com"

        with patch.object(roxy, "_wait_for_email_input", side_effect=find_input), patch.object(
            roxy,
            "_human_type_text",
            side_effect=lambda *args, **kwargs: events.append("type_email"),
        ), patch.object(
            roxy,
            "_email_input_value_state",
            return_value={"inputs": [{"value": "roxy@example.com"}]},
        ), patch.object(
            roxy,
            "_submit_email_step",
            side_effect=lambda *args, **kwargs: events.append("submit_email"),
        ), patch.object(
            roxy,
            "_wait_email_submit_next_state",
            return_value="otp",
        ), patch.object(roxy, "human_delay"), patch.object(roxy, "_check_manual_stop"):
            result = roxy._submit_email_and_wait_next(
                object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "otp")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )

    def test_browser_use_finds_input_before_allocating_email(self):
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "browser@example.com"

        with patch.object(browser_use, "_wait_for_email_input_pw", side_effect=find_input), patch.object(
            browser_use,
            "_human_fill_locator",
            side_effect=lambda *args, **kwargs: events.append("type_email"),
        ), patch.object(
            browser_use,
            "_submit_email_step_pw",
            side_effect=lambda *args, **kwargs: events.append("submit_email") or True,
        ), patch.object(
            browser_use,
            "_wait_after_email_submit_transition",
            return_value="email_verification",
        ), patch.object(browser_use, "_human_pause"), patch.object(
            browser_use, "_check_manual_stop"
        ), patch.object(browser_use, "_page_url", return_value="https://chatgpt.com/auth/login"):
            result = browser_use._submit_email_until_transition(
                object(), object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "email_verification")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )


if __name__ == "__main__":
    unittest.main()

class JobEmailCleanupFallbackTests(unittest.TestCase):
    """stall/超时收尾漏标邮箱的回归：局部变量为空时从 job 记录兜底取邮箱。"""

    def test_cleanup_email_falls_back_to_job_record_when_local_empty(self):
        with patch.object(registration_service.db, "get_job", return_value={"email": "job@example.com"}):
            self.assertEqual(
                registration_service._resolve_job_email_for_cleanup(7),
                "job@example.com",
            )
            self.assertEqual(
                registration_service._resolve_job_email_for_cleanup(7, "explicit@example.com"),
                "explicit@example.com",
            )
        with patch.object(registration_service.db, "get_job", return_value=None):
            self.assertEqual(
                registration_service._resolve_job_email_for_cleanup(7, None, ""),
                "",
            )


if __name__ == "__main__":
    unittest.main()

class EmailSubmittedMarkerTests(unittest.TestCase):
    """已提交邮箱判定：失败回收回 available 还是 failed 的依据。"""

    def test_submitted_marker_detection(self):
        import os
        import tempfile

        submitted_path = clean_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as fh:
                fh.write("11:25:34 [Cloak注册] 已提交邮箱，等待进入密码页或验证码页（1/1）\n")
                submitted_path = fh.name
            with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as fh:
                fh.write("11:25:14 [Cloak注册][预检] attempt=1/1 ok=True country=SG\n")
                clean_path = fh.name
            self.assertTrue(registration_service._registration_email_was_submitted(submitted_path))
            self.assertFalse(registration_service._registration_email_was_submitted(clean_path))
            self.assertFalse(registration_service._registration_email_was_submitted(None))
            self.assertFalse(registration_service._registration_email_was_submitted("Z:/no/such/file.log"))
        finally:
            for p in filter(None, (submitted_path, clean_path)):
                os.unlink(p)
