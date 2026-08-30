# -*- coding: utf-8 -*-
import gzip
import json
import unittest
from pathlib import Path

from flask import Response

from webui import app as webui_app


class UpstreamPerformanceCompatibilityTests(unittest.TestCase):
    def _large_json_response(self):
        payload = {"items": [{"id": index, "value": "x" * 80} for index in range(40)]}
        return Response(json.dumps(payload), mimetype="application/json")

    def test_json_response_is_compressed_when_client_accepts_gzip(self):
        response = webui_app._maybe_compress_json_response(
            self._large_json_response(),
            "gzip, deflate",
        )

        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(response.get_data())), {"items": [{"id": index, "value": "x" * 80} for index in range(40)]})
        self.assertIn("Accept-Encoding", response.headers.get("Vary", ""))

    def test_json_response_is_not_compressed_without_explicit_gzip_support(self):
        response = webui_app._maybe_compress_json_response(
            self._large_json_response(),
            "identity",
        )

        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertTrue(response.get_data().startswith(b"{"))

    def test_json_response_honors_gzip_q_zero(self):
        response = webui_app._maybe_compress_json_response(
            self._large_json_response(),
            "gzip;q=0, identity;q=1",
        )

        self.assertIsNone(response.headers.get("Content-Encoding"))

    def test_both_webui_templates_use_lower_frequency_log_polling(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("index.html", "index_legacy.html"):
            template = (root / "webui" / "templates" / name).read_text(encoding="utf-8")
            self.assertIn("setInterval(pollLog, 5000)", template)
            self.assertIn("setInterval(pollRetryLog, 5000)", template)
            self.assertIn("setInterval(pollLiveLog, 5000)", template)
            self.assertNotIn("setInterval(pollLog, 2000)", template)
            self.assertNotIn("setInterval(pollRetryLog, 2000)", template)
            self.assertNotIn("setInterval(pollLiveLog, 2000)", template)


if __name__ == "__main__":
    unittest.main()
