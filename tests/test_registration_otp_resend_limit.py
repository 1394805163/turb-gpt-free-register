import logging
import sys
import types
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from core import browser_use_registration, cloakbrowser_registration, icloud_mail_client, roxy_registration


ALIAS = "long-private-alias@icloud.com"


class RegistrationOtpResendLimitTests(unittest.TestCase):
    @staticmethod
    def _otp_wait_session() -> Mock:
        session = Mock()
        session.wait.side_effect = [
            TimeoutError(f"first fixture failure for {ALIAS}"),
            TimeoutError(f"second fixture failure for {ALIAS}"),
            "123456",
        ]
        return session

    def test_cloak_failure_failure_success_resends_only_once(self):
        module = cloakbrowser_registration
        driver = Mock()
        opened = SimpleNamespace(profile_id="cloak-profile", raw={})
        pool = Mock()
        pool.wait_for_code.side_effect = [None, None, "123456"]
        resend = Mock()
        patches = {
            "build_cloak_driver": patch.object(module, "build_cloak_driver", return_value=(driver, opened)),
            "wait_for_otp": patch.object(module, "wait_for_otp", icloud_mail_client.fetch_latest_otp),
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
            stack.enter_context(patch.object(icloud_mail_client, "_pool", return_value=pool))
            stack.enter_context(patch.object(module._cfg, "CLOAK_KEEP_BROWSER_OPEN", False))
            stack.enter_context(patch.object(module._twofa_cfg, "ENABLE_2FA", False))
            stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
            stack.enter_context(patch("core.email_provider.release_email"))
            with self.assertLogs("core.cloakbrowser_registration", level=logging.INFO) as captured:
                result = module.run_cloak_registration(ALIAS, "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(pool.wait_for_code.call_count, 3)
        self.assertEqual(resend.call_count, 1)
        full_alias_logged = ALIAS in "\n".join(captured.output)
        self.assertFalse(full_alias_logged)

    def test_cloak_stalled_submit_retries_same_code_without_entering_profile_early(self):
        module = cloakbrowser_registration
        driver = Mock()
        opened = SimpleNamespace(profile_id="cloak-profile", raw={})
        submit = Mock()
        wait_after_submit = Mock(side_effect=["stalled", "accepted"])
        with ExitStack() as stack:
            for item in (
                patch.object(module, "build_cloak_driver", return_value=(driver, opened)),
                patch.object(module, "_assert_login_gate_not_blocked"),
                patch.object(module, "_maybe_accept"),
                patch.object(module, "_check_manual_stop"),
                patch.object(module, "_submit_email_and_wait_next", return_value="otp"),
                patch.object(module, "OtpWaitSession", return_value=SimpleNamespace(wait=Mock(return_value="123456"), mark_used=Mock())),
                patch.object(module, "_clear_otp_inputs"),
                patch.object(module, "_type_otp"),
                patch.object(module, "_click_continue", submit),
                patch.object(module, "_wait_after_email_otp_submit", wait_after_submit),
                patch.object(module, "_complete_profile_page", return_value=False),
                patch.object(module, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token"}),
                patch.object(module, "save_account_data", return_value=1),
                patch.object(module, "resolve_email_source", return_value="icloud"),
                patch.object(module, "human_delay"),
                patch.object(module._cfg, "CLOAK_KEEP_BROWSER_OPEN", False),
                patch.object(module._twofa_cfg, "ENABLE_2FA", False),
                patch("config.codex.ENABLE_CODEX_AUTO", False),
            ):
                stack.enter_context(item)
            result = module.run_cloak_registration(ALIAS, "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(wait_after_submit.call_count, 2)

    def test_otp_page_without_transition_is_stalled_not_accepted(self):
        module = roxy_registration
        driver = Mock()
        with patch.object(module, "_is_email_verification_page", return_value=True):
            outcome = module._wait_after_email_otp_submit(driver, timeout=0)
        self.assertEqual(outcome, "stalled")

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
            with self.assertLogs("core.roxy_registration", level=logging.INFO) as captured:
                result = module.run_roxy_registration(ALIAS, "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(otp_session.wait.call_count, 3)
        self.assertEqual(resend.call_count, 1)
        full_alias_logged = ALIAS in "\n".join(captured.output)
        self.assertFalse(full_alias_logged)

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
        type_email = Mock()
        wait_after_email_submit = Mock(side_effect=["email_verification", "email_page", "email_verification"])
        wait_otp = Mock(
            side_effect=[
                TimeoutError(f"first fixture failure for {ALIAS}"),
                TimeoutError(f"second fixture failure for {ALIAS}"),
                "123456",
            ]
        )

        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"playwright": playwright_package, "playwright.sync_api": sync_api}))
            for item in (
                patch.object(module, "BrowserUseClient", return_value=client),
                patch.object(module, "_apply_cloud_browser_automation_mask"),
                patch.object(module, "_maybe_accept_cookies"),
                patch.object(module, "_check_manual_stop"),
                patch.object(module, "_type_email", type_email),
                patch.object(module, "_wait_after_email_submit_transition", wait_after_email_submit),
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
            with self.assertLogs("core.browser_use_registration", level=logging.INFO) as captured:
                result = module.run_browser_use_registration(ALIAS, "Fixture", "1990-01-01")

        self.assertTrue(result["success"], result)
        self.assertEqual(wait_otp.call_count, 3)
        actual_submissions = type_email.call_count - 1
        self.assertEqual(actual_submissions, 1)
        full_alias_logged = ALIAS in "\n".join(captured.output)
        self.assertFalse(full_alias_logged)


if __name__ == "__main__":
    unittest.main()
