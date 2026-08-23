# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from core import icloud_mail_client
from webui.app import create_app


class ICloudPoolManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mailbox_file = root / "icloud_mailboxes.txt"
        self.state_file = root / "icloud_mailboxes.json"
        self.path_patch = patch.object(email_config, "ICLOUD_MAILBOXES_FILE", str(self.mailbox_file))
        self.state_patch = patch.object(icloud_mail_client, "_STATE_FILE", self.state_file)
        self.path_patch.start()
        self.state_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.state_patch.stop)

    def test_import_list_status_and_delete_keep_text_and_state_in_sync(self):
        result = icloud_mail_client.import_mailboxes(
            "first@icloud.com----主力\nsecond@icloud.com\nfirst@icloud.com----重复"
        )

        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["skipped"], 1)
        rows = icloud_mail_client.list_mailboxes()
        self.assertEqual([row["email"] for row in rows], ["first@icloud.com", "second@icloud.com"])
        self.assertEqual(rows[0]["label"], "主力")
        self.assertEqual(rows[0]["status"], "available")

        icloud_mail_client.set_mailbox_status("first@icloud.com", "failed", "手动标记")
        failed = icloud_mail_client.list_mailboxes(status="failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["note"], "手动标记")

        icloud_mail_client.set_mailbox_status("first@icloud.com", "available")
        self.assertEqual(icloud_mail_client.mailbox_summary()["available"], 2)

        self.assertTrue(icloud_mail_client.delete_mailbox("second@icloud.com"))
        self.assertEqual(icloud_mail_client.mailbox_summary()["total"], 1)
        self.assertNotIn("second@icloud.com", self.mailbox_file.read_text(encoding="utf-8"))

    def test_webui_can_import_and_list_icloud_pool(self):
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        imported = client.post(
            "/api/outlook/import",
            json={"source": "icloud", "text": "alias@icloud.com----测试", "as_registered": True},
        )
        self.assertEqual(imported.status_code, 200)
        self.assertFalse(imported.get_json()["as_registered"])

        listed = client.get("/api/outlook?source=icloud&paged=1&page=1&page_size=20")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["items"][0]["source"], "icloud")
        self.assertEqual(listed.get_json()["items"][0]["label"], "测试")

    def test_sync_registered_mailboxes_marks_existing_and_inserts_missing(self):
        icloud_mail_client.import_mailboxes("existing@icloud.com----保留标签")
        result = icloud_mail_client.sync_registered_mailboxes([
            {"email": "existing@icloud.com", "email_source": "icloud"},
            {"email": "missing@icloud.com", "email_source": "icloud"},
        ])
        self.assertEqual(result["accounts"], 2)
        self.assertEqual(result["marked_used"], 1)
        self.assertEqual(result["inserted"], 1)
        rows = {row["email"]: row for row in icloud_mail_client.list_mailboxes()}
        self.assertEqual(rows["existing@icloud.com"]["status"], "used")
        self.assertEqual(rows["existing@icloud.com"]["label"], "保留标签")
        self.assertEqual(rows["missing@icloud.com"]["status"], "used")


if __name__ == "__main__":
    unittest.main()
