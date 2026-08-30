import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import cloakbrowser_liveness


ALIAS = "alias@icloud.com"


class CloakBrowserLivenessOtpTests(unittest.TestCase):
    def _run_flow(self, *, otp_session, resend, logged_in, outcome="accepted"):
        module = cloakbrowser_liveness
        driver = Mock()
        opened = SimpleNamespace(raw={})
        with ExitStack() as stack:
            for patcher in (
                patch.object(module, "build_cloak_driver", return_value=(driver, opened)),
                patch.object(module, "_maybe_accept"),
                patch.object(module, "human_delay"),
                patch.object(module, "_submit_email_and_wait_next", return_value="otp"),
                patch.object(module, "_fill_password_page_if_present"),
                patch.object(module, "_is_email_verification_page", return_value=True),
                patch.object(module, "_is_chatgpt_logged_in_page", side_effect=logged_in),
                patch.object(module, "OtpWaitSession", return_value=otp_session),
                patch.object(module, "_click_resend_email_otp", resend),
                patch.object(module, "_clear_otp_inputs"),
                patch.object(module, "_type_otp"),
                patch.object(module, "_click_continue"),
                patch.object(module, "_wait_after_email_otp_submit", return_value=outcome),
                patch.object(module, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token"}),
                patch.object(module.cloak_cfg, "CLOAK_KEEP_BROWSER_OPEN", False),
            ):
                stack.enter_context(patcher)
            return module.run_cloak_liveness_flow(ALIAS)

    def test_otp_timeout_resends_once_and_continues_waiting(self):
        otp_session = Mock()
        otp_session.wait.side_effect = [TimeoutError("fixture timeout"), "123456"]
        resend = Mock(return_value={"ok": True})

        result = self._run_flow(
            otp_session=otp_session,
            resend=resend,
            logged_in=[False, False],
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(otp_session.wait.call_count, 2)
        self.assertEqual(resend.call_count, 1)

    def test_invalid_otp_with_logged_in_page_skips_resend(self):
        otp_session = Mock()
        otp_session.wait.return_value = "123456"
        resend = Mock(return_value={"ok": True})

        result = self._run_flow(
            otp_session=otp_session,
            resend=resend,
            logged_in=[True],
            outcome="invalid",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(otp_session.wait.call_count, 1)
        resend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
