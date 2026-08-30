import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from core.icloud_mail_pool import ICloudMailboxPool
from tests.test_icloud_mail_pool import ScriptedIMAP, otp_mail


def mail_without_code(address: str) -> bytes:
    message = EmailMessage()
    message["From"] = "OpenAI <noreply@tm.openai.com>"
    message["To"] = address
    message["Date"] = datetime.now(timezone.utc)
    message["Message-ID"] = f"<{address}-pending@fixture.test>"
    message["Subject"] = "Your ChatGPT security notification"
    message.set_content("Your request is being processed.")
    return message.as_bytes()


class ICloudMailboxPoolRegressionTests(unittest.TestCase):
    def pool(self, **overrides) -> ICloudMailboxPool:
        config = {
            "message_limit": 20,
            "initial_scan_limit": 2,
            "wait_timeout": 0.05,
            "wait_interval": 0,
            "reselect_interval": 30,
            "reconnect_interval": 30,
            "clock_skew_seconds": 30,
        }
        config.update(overrides)
        return ICloudMailboxPool(config, Path("unused.json"))

    @staticmethod
    def mailbox(address: str) -> dict:
        return {
            "address": address,
            "_code_not_before": datetime.now(timezone.utc),
            "_seen": set(),
        }

    def test_uid_outside_initial_window_is_backfilled(self):
        target = "alias@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"8", b"9", b"10"]],
            messages={b"8": otp_mail(target, "123456")},
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "123456")
        fetched_uids = [args[0] for command, args in imap.uid_calls if command == "fetch"]
        self.assertIn(b"8", fetched_uids)

    def test_target_message_without_code_is_retried_until_code_appears(self):
        target = "alias@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"12"]],
            messages={},
            fetch_sequences={b"12": [mail_without_code(target), otp_mail(target, "654321")]},
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "654321")
        self.assertEqual(imap.fetch_counts[b"12"], 2)


if __name__ == "__main__":
    unittest.main()
