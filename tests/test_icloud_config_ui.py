# -*- coding: utf-8 -*-
import unittest

from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


class ICloudConfigUiTests(unittest.TestCase):
    def test_icloud_fields_are_editable_from_webui(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}

        self.assertIn("ICLOUD_MAILBOXES_FILE", fields)
        self.assertIn("ICLOUD_IMAP_USERNAME", fields)
        self.assertIn("ICLOUD_IMAP_PASSWORD", fields)
        self.assertEqual(fields["ICLOUD_IMAP_PASSWORD"].get("storage"), "env")
        self.assertTrue(fields["ICLOUD_IMAP_PASSWORD"].get("secret"))

    def test_icloud_password_is_registered_as_secret(self):
        self.assertIn("ICLOUD_IMAP_PASSWORD", SECRET_ENV_KEYS)

    def test_email_source_help_mentions_icloud(self):
        source = next(item for item in EDITABLE_FIELDS if item["key"] == "EMAIL_SOURCE")

        self.assertIn("icloud", source["help"].lower())


if __name__ == "__main__":
    unittest.main()
