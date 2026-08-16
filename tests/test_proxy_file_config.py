# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_get_proxy_pool_hot_reloads_atomic_file_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "verified.txt"
            source.write_text("http://192.0.2.31:8031\n", encoding="utf-8")
            with patch.object(proxy, "PROXY_POOL_FILE", str(source)), patch.object(
                proxy, "REGISTRATION_PROXY_REQUIRED", True
            ):
                first = proxy.get_proxy_pool()
                replacement = source.with_suffix(".tmp")
                replacement.write_text(
                    "http://192.0.2.32:8032\nhttp://192.0.2.33:8033\n",
                    encoding="utf-8",
                )
                replacement.replace(source)
                second = proxy.get_proxy_pool()

        self.assertEqual(first, ["http://192.0.2.31:8031"])
        self.assertEqual(second, [
            "http://192.0.2.32:8032",
            "http://192.0.2.33:8033",
        ])

    def test_get_proxy_pool_required_gate_does_not_fallback_when_file_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "verified.txt"
            source.write_text("# no qualified nodes\n", encoding="utf-8")
            with patch.object(proxy, "PROXY_POOL_FILE", str(source)), patch.object(
                proxy, "REGISTRATION_PROXY_REQUIRED", True
            ):
                result = proxy.get_proxy_pool()

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
