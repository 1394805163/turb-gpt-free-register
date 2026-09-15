# -*- coding: utf-8 -*-
import unittest
from core.protocol_registration import run_protocol_registration


class ProtocolRegistrationTests(unittest.TestCase):
    def test_empty_email_skipped(self):
        r = run_protocol_registration("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "skipped")

    def test_whitespace_email_skipped(self):
        r = run_protocol_registration("   ")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
