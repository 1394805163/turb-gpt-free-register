import tempfile
import unittest
from pathlib import Path

from core.email_provider import parse_email_sources
from core.icloud_mail_pool import ICloudMailboxPool


class FakeIMAP:
    def __init__(self, target: bytes) -> None:
        self.target = target
        self.calls = []

    def uid(self, command, *_args):
        self.calls.append((command, _args))
        return "OK", [self.target]


class ICloudMailboxPoolTests(unittest.TestCase):
    def test_targeted_search_never_falls_back_to_all_messages(self):
        pool = ICloudMailboxPool({"message_limit": 2}, Path("unused.json"))
        imap = FakeIMAP(b"10 11 12")

        self.assertEqual(pool._candidate_uids(imap, "alias@icloud.com"), [b"11", b"12"])
        self.assertTrue(all("ALL" not in call[1] for call in imap.calls))

    def test_pool_claim_and_success_state_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            pool = ICloudMailboxPool(
                {"mailboxes": "first@icloud.com\nsecond@icloud.com"},
                state_file,
            )
            mailbox = pool.acquire()
            pool.finish(mailbox, True)

            self.assertEqual(mailbox["address"], "first@icloud.com")
            self.assertEqual(pool.acquire()["address"], "second@icloud.com")

    def test_email_provider_accepts_icloud_source(self):
        self.assertEqual(parse_email_sources("icloud,outlook"), ["icloud", "outlook"])


if __name__ == "__main__":
    unittest.main()
