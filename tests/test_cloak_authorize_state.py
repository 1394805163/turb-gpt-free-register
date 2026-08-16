import unittest
from unittest.mock import patch

from core import roxy_registration


class _AuthorizeDriver:
    current_url = "https://auth.openai.com/api/accounts/authorize?client_id=fixture"

    def execute_script(self, script, *args):
        if 'input[type="email"]' in script:
            return {"url": self.current_url, "inputs": []}
        return {}


class _EmptyNavigationDriver:
    current_url = ""

    def execute_script(self, script, *args):
        if 'input[type="email"]' in script:
            return {"url": "", "inputs": []}
        return {}


class CloakAuthorizeStateTests(unittest.TestCase):
    def test_authorize_intermediate_state_is_explicit(self):
        self.assertTrue(
            roxy_registration._is_authorize_intermediate_url(
                "https://auth.openai.com/api/accounts/authorize?state=fixture"
            )
        )
        self.assertFalse(
            roxy_registration._is_authorize_intermediate_url(
                "https://auth.openai.com/about-you"
            )
        )

    def test_stuck_authorize_page_returns_without_full_email_timeout(self):
        driver = _AuthorizeDriver()
        ticks = iter([0.0, 0.0, 0.0, 5.0, 16.0])
        with patch.object(roxy_registration.time, "time", side_effect=lambda: next(ticks, 16.0)), patch.object(
            roxy_registration.time, "sleep", return_value=None
        ), patch.object(roxy_registration, "_has_access_token", return_value=False), patch.object(
            roxy_registration, "_is_login_password_page", return_value=False
        ), patch.object(roxy_registration, "_is_email_verification_page", return_value=False), patch.object(
            roxy_registration, "_is_signup_password_page", return_value=False
        ):
            state = roxy_registration._wait_email_submit_next_state(driver, "fixture@example.test", timeout=60)
        self.assertEqual(state, "authorize_timeout")

    def test_empty_url_navigation_returns_without_full_email_timeout(self):
        driver = _EmptyNavigationDriver()
        ticks = iter([0.0, 0.0, 0.0, 5.0, 13.0])
        with patch.object(roxy_registration.time, "time", side_effect=lambda: next(ticks, 13.0)), patch.object(
            roxy_registration.time, "sleep", return_value=None
        ), patch.object(roxy_registration, "_has_access_token", return_value=False), patch.object(
            roxy_registration, "_is_login_password_page", return_value=False
        ), patch.object(roxy_registration, "_is_email_verification_page", return_value=False), patch.object(
            roxy_registration, "_is_signup_password_page", return_value=False
        ):
            state = roxy_registration._wait_email_submit_next_state(driver, "fixture@example.test", timeout=60)
        self.assertEqual(state, "navigation_timeout")


if __name__ == "__main__":
    unittest.main()
