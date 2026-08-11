import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.account_liveness import _validate_with_retry
from core.email_provider import OtpWaitSession
from core.icloud_mail_client import fetch_latest_otp
from core.icloud_mail_pool import ICloudMailboxPool
from core.openai_auth import EmailOtpInvalidError
from tests.test_icloud_mail_pool import ScriptedIMAP, otp_mail


class ICloudOtpRetryTests(unittest.TestCase):
    def test_otp_wait_session_shares_dedupe_and_consumes_one_total_budget(self):
        wait_fn = Mock(side_effect=["934567", "945678"])
        with patch("core.email_provider.time.monotonic", side_effect=[100.0, 100.0, 112.0]):
            session = OtpWaitSession(max_wait=120, wait_fn=wait_fn)
            first = session.wait("alias@icloud.com", after_ts=1.0)
            session.mark_used(first)
            session.wait("alias@icloud.com", after_ts=2.0)

        first_kwargs = wait_fn.call_args_list[0].kwargs
        second_kwargs = wait_fn.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_wait"], 120)
        self.assertEqual(second_kwargs["max_wait"], 108)
        self.assertIn(first, second_kwargs["used_codes"])
        self.assertIs(first_kwargs["otp_state"], second_kwargs["otp_state"])
    def test_mail_date_inside_thirty_second_clock_skew_is_accepted(self):
        target = "alias@icloud.com"
        requested_at = datetime.now(timezone.utc)
        imap = ScriptedIMAP(
            snapshots=[[b"31"]],
            messages={b"31": otp_mail(target, "789012", received=requested_at - timedelta(seconds=20))},
        )
        pool = ICloudMailboxPool(
            {
                "wait_timeout": 0.05,
                "wait_interval": 0,
                "reselect_interval": 30,
                "reconnect_interval": 30,
                "clock_skew_seconds": 30,
            },
            Path("unused.json"),
        )
        mailbox = {"address": target, "_code_not_before": requested_at, "_seen": set()}

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(mailbox)

        self.assertEqual(code, "789012")

    def test_fetch_rounds_share_uid_message_id_and_code_hash_dedupe_state(self):
        target = "alias@icloud.com"
        first_imap = ScriptedIMAP(
            snapshots=[[b"40"]],
            messages={b"40": otp_mail(target, "890123")},
        )
        second_imap = ScriptedIMAP(
            snapshots=[[b"40", b"41"]],
            messages={
                b"40": otp_mail(target, "890123"),
                b"41": otp_mail(target, "901234"),
            },
        )
        pool = ICloudMailboxPool(
            {
                "wait_timeout": 0.05,
                "wait_interval": 0,
                "reselect_interval": 30,
                "reconnect_interval": 30,
                "clock_skew_seconds": 30,
            },
            Path("unused.json"),
        )
        otp_state: dict[str, set] = {}

        with patch("core.icloud_mail_client._pool", return_value=pool), patch.object(
            pool, "_connect_imap", side_effect=[first_imap, second_imap]
        ):
            first = fetch_latest_otp(target, 0.0, otp_state=otp_state)
            second = fetch_latest_otp(target, 0.0, used_codes={first}, otp_state=otp_state)

        self.assertEqual(first, "890123")
        self.assertEqual(second, "901234")
        self.assertIn(b"40", otp_state["seen_uids"])
        self.assertGreaterEqual(len(otp_state["seen_message_ids"]), 2)
        self.assertEqual(len(otp_state["seen_code_hashes"]), 2)
        self.assertEqual(otp_state["last_uid"], 41)
        self.assertEqual(otp_state["uidvalidity"], "1")

    def test_uidvalidity_change_clears_shared_uid_state_before_reused_uid(self):
        target = "alias@icloud.com"
        first_imap = ScriptedIMAP(
            snapshots=[[b"50"]],
            messages={b"50": otp_mail(target, "912345")},
            uidvalidity=b"10",
        )
        second_imap = ScriptedIMAP(
            snapshots=[[b"50"]],
            messages={b"50": otp_mail(target, "923456")},
            uidvalidity=b"11",
        )
        pool = ICloudMailboxPool(
            {"wait_timeout": 0.05, "wait_interval": 0, "clock_skew_seconds": 30},
            Path("unused.json"),
        )
        otp_state: dict[str, object] = {}

        with patch("core.icloud_mail_client._pool", return_value=pool), patch.object(
            pool, "_connect_imap", side_effect=[first_imap, second_imap]
        ):
            first = fetch_latest_otp(target, 0.0, otp_state=otp_state)
            second = fetch_latest_otp(target, 0.0, used_codes={first}, otp_state=otp_state)

        self.assertEqual(second, "923456")
        self.assertEqual(otp_state["uidvalidity"], "11")

    def test_login_retry_rejects_already_used_otp_and_resends_only_once(self):
        with patch("core.account_liveness.wait_for_otp", side_effect=["111111", "222222"]) as wait, patch(
            "core.account_liveness.validate_email_otp",
            side_effect=[EmailOtpInvalidError("expired"), {"continue_url": "https://chatgpt.com/callback"}],
        ), patch("core.account_liveness.send_email_otp") as resend, patch(
            "core.account_liveness.time.sleep", return_value=None
        ):
            result = _validate_with_retry(Mock(), "alias@icloud.com", 100.0)

        self.assertEqual(result["continue_url"], "https://chatgpt.com/callback")
        self.assertEqual(resend.call_count, 1)
        self.assertEqual(wait.call_count, 2)
        self.assertIn("111111", wait.call_args_list[1].kwargs["used_codes"])
        self.assertIs(wait.call_args_list[0].kwargs["otp_state"], wait.call_args_list[1].kwargs["otp_state"])


if __name__ == "__main__":
    unittest.main()
