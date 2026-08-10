# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from config import proxy


class ProxyFileConfigTests(unittest.TestCase):
    def test_load_proxy_pool_file_normalizes_filters_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "verified.txt"
            source.write_text(
                "\n".join([
                    "# generated proxy list",
                    "192.0.2.10:8080",
                    "http://192.0.2.11:8081",
                    "socks5h://user:pass@192.0.2.12:1080",
                    "http://192.0.2.11:8081",
                    "ftp://192.0.2.13:21",
                    "not-a-proxy",
                ]),
                encoding="utf-8",
            )

            result = proxy.load_proxy_pool_file(source)

        self.assertEqual(result, [
            "http://192.0.2.10:8080",
            "http://192.0.2.11:8081",
            "socks5h://user:pass@192.0.2.12:1080",
        ])

    def test_resolve_proxy_pool_prefers_nonempty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "verified.txt"
            source.write_text("https://192.0.2.20:8443\n", encoding="utf-8")

            result, loaded_from = proxy.resolve_proxy_pool(
                ["http://127.0.0.1:7897"],
                source,
            )

        self.assertEqual(result, ["https://192.0.2.20:8443"])
        self.assertEqual(loaded_from, source.resolve())

    def test_resolve_proxy_pool_falls_back_when_file_is_missing_or_empty(self):
        configured = ["http://127.0.0.1:7897"]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "missing.txt"
            result, loaded_from = proxy.resolve_proxy_pool(configured, source)

        self.assertEqual(result, configured)
        self.assertIsNone(loaded_from)


if __name__ == "__main__":
    unittest.main()
