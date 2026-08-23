# -*- coding: utf-8 -*-
import time
import unittest
from unittest.mock import patch

from core import db


class DbViewerDebounceTests(unittest.TestCase):
    def test_static_viewer_refresh_is_coalesced(self):
        with db._VIEWER_REFRESH_LOCK:
            if db._VIEWER_REFRESH_TIMER is not None:
                db._VIEWER_REFRESH_TIMER.cancel()
                db._VIEWER_REFRESH_TIMER = None
            db._VIEWER_REFRESH_GENERATION += 1
        with patch.object(db, "_VIEWER_DEBOUNCE_SECONDS", 0.02), patch.object(
            db, "_render_static_viewer", return_value=None
        ) as render:
            db._schedule_static_viewer_refresh("first")
            db._schedule_static_viewer_refresh("second")
            time.sleep(0.08)

        self.assertEqual(render.call_count, 1)


if __name__ == "__main__":
    unittest.main()
