import sys
import types
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from core import browser_use_registration, cloakbrowser_registration, roxy_registration


class RegistrationOtpResendLimitTests(unittest.TestCase):
    @staticmethod
    def _otp_wait_session() -> Mock:
        session = Mock()
        session.wait.side_effect = [TimeoutError("first fixture failure"), TimeoutError("second fixture failure"), "123456"]
        return session

    def test_cloak_failure_failure_success_resends_only_once(self):
        module = cloakbrowser_registration
        driver = Mock()
        opened = SimpleNamespace(profile_id="cloak-profile", raw={})
        otp_session = self._otp_wait_session()
        resend = Mock()
        patches = {
            "build_cloak_driver": patch.object(module, "build_cloak_driver", return_value=(driver, opened)),
            "OtpWaitSession": patch.object(module, "OtpWaitSession", return_value=otp_session),
            "_maybe_accept": patch.object(module, "_maybe_accept"),
            "_check_manual_stop": patch.object(module, "_check_manual_stop"),
            "_submit_email_and_wait_next": patch.object(module, "_submit_email_and_wait_next", return_value="otp"),
            "_click_resend_email_otp": patch.object(module, "_click_resend_email_otp", resend),
            "_clear_otp_inputs": patch.object(module, "_clear_otp_inputs"),
            "_type_otp": patch.object(module, "_type_otp"),
            "_click_continue": patch.object(module, "_click_continue"),
            "_wait_after_email_otp_submit": patch.object(module, "_wait_after_email_otp_submit", return_value="accepted"),
            "_complete_profile_page": patch.object(module, "_complete_profile_page", return_value=False),
            "_fetch_chatgpt_session": patch.object(module, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token"}),
            "save_account_data": patch.object(module, "save_account_data", return_value=1),
            "resolve_email_source": patch.object(module, "resolve_email_source", return_value="icloud"),
            "human_delay": patch.object(module, "human_delay"),
        }
        with ExitStack() as stack:
            for item in patches.values():
                stack.enter_context(item)
            stack.enter_context(patch.object(module._cfg, "CLOAK_KEEP_BROWSER_OPEN", False))
            stack.enter_context(patch.object(module._twofa_cfg, "ENABLE_2FA", False))
            stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
            stack.enter_context(patch("core.email_provider.release_email"))
            result = module.run_cloak_registration("alias@icloud.com", "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(otp_session.wait.call_count, 3)
        self.assertEqual(resend.call_count, 1)

    def test_roxy_failure_failure_success_resends_only_once(self):
        module = roxy_registration
        client = Mock()
        opened = SimpleNamespace(profile_id="roxy-profile", raw={})
        client.open_profile.return_value = opened
        driver = Mock()
        otp_session = self._otp_wait_session()
        resend = Mock()
        with ExitStack() as stack:
            for item in (
                patch.object(module, "RoxyBrowserClient", return_value=client),
                patch.object(module, "_build_driver", return_value=driver),
                patch.object(module, "OtpWaitSession", return_value=otp_session),
                patch.object(module, "_center_browser_window"),
                patch.object(module, "_safe_get"),
                patch.object(module, "_page_warmup"),
                patch.object(module, "_maybe_accept"),
                patch.object(module, "_check_manual_stop"),
                patch.object(module, "_submit_email_and_wait_next", return_value="otp"),
                patch.object(module, "_click_resend_email_otp", resend),
                patch.object(module, "_clear_otp_inputs"),
                patch.object(module, "_type_otp"),
                patch.object(module, "_click_continue"),
                patch.object(module, "_wait_after_email_otp_submit", return_value="accepted"),
                patch.object(module, "_complete_profile_page", return_value=False),
                patch.object(module, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token"}),
                patch.object(module, "save_account_data", return_value=1),
                patch.object(module, "resolve_email_source", return_value="icloud"),
                patch.object(module, "human_delay"),
                patch.object(module._cfg, "ROXY_KEEP_BROWSER_OPEN", False),
                patch.object(module._twofa_cfg, "ENABLE_2FA", False),
                patch("config.codex.ENABLE_CODEX_AUTO", False),
            ):
                stack.enter_context(item)
            result = module.run_roxy_registration("alias@icloud.com", "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(otp_session.wait.call_count, 3)
        self.assertEqual(resend.call_count, 1)

    def test_browser_use_failure_failure_success_restarts_otp_only_once(self):
        module = browser_use_registration
        client = Mock()
        opened = SimpleNamespace(
            connect_url="ws://fixture",
            proxy_country_code="US",
            profile_id="browser-use-profile",
            session_id="",
            raw={},
        )
        client.open_session.return_value = opened
        page = Mock()
        context = Mock()
        context.pages = [page]
        browser = Mock()
        browser.contexts = [context]
        playwright = SimpleNamespace(chromium=Mock())
        playwright.chromium.connect_over_cdp.return_value = browser
        playwright_context = MagicMock()
        playwright_context.__enter__.return_value = playwright
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=playwright_context)
        playwright_package = types.ModuleType("playwright")
        submit_email = Mock()
        wait_otp = Mock(side_effect=[TimeoutError("first fixture failure"), TimeoutError("second fixture failure"), "123456"])

        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"playwright": playwright_package, "playwright.sync_api": sync_api}))
            for item in (
                patch.object(module, "BrowserUseClient", return_value=client),
                patch.object(module, "_apply_cloud_browser_automation_mask"),
                patch.object(module, "_maybe_accept_cookies"),
                patch.object(module, "_check_manual_stop"),
                patch.object(module, "_submit_email_until_transition", submit_email),
                patch.object(module, "_fill_password_if_present", return_value=None),
                patch.object(module, "_assert_not_external_idp"),
                patch.object(module, "_pick_live_page", return_value=page),
                patch.object(module, "_browser_use_heartbeat", return_value=page),
                patch.object(module, "_quick_auth_state", return_value={"state": "email_verification", "url": "https://auth.openai.com/email-verification"}),
                patch.object(module, "_wait_for_otp_with_browser_heartbeat", wait_otp),
                patch.object(module, "_clear_otp_inputs"),
                patch.object(module, "_type_otp"),
                patch.object(module, "_click_continue"),
                patch.object(module, "_wait_after_otp", return_value="accepted"),
                patch.object(module, "_complete_profile_page", return_value=False),
                patch.object(module, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token"}),
                patch.object(module, "save_account_data", return_value=1),
                patch.object(module, "resolve_email_source", return_value="icloud"),
                patch.object(module, "_bu_delay"),
                patch.object(module, "_page_url", return_value="https://auth.openai.com/email-verification"),
                patch.object(module._twofa_cfg, "ENABLE_2FA", False),
                patch("config.codex.ENABLE_CODEX_AUTO", False),
            ):
                stack.enter_context(item)
            result = module.run_browser_use_registration("alias@icloud.com", "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(wait_otp.call_count, 3)
        self.assertEqual(submit_email.call_count - 1, 1)


if __name__ == "__main__":
    unittest.main()
