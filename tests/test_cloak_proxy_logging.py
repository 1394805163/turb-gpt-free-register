# -*- coding: utf-8 -*-
import unittest

from core.cloakbrowser_driver import _proxy_log_label


class CloakProxyLoggingTests(unittest.TestCase):
    def test_proxy_log_label_hides_credentials(self):
        label = _proxy_log_label("http://user:password@127.0.0.1:2260")

        self.assertNotIn("user", label)
        self.assertNotIn("password", label)
        self.assertEqual(label, "http://127.0.0.1:2260")

    def test_empty_proxy_has_clear_label(self):
        self.assertEqual(_proxy_log_label(None), "无")


if __name__ == "__main__":
    unittest.main()
