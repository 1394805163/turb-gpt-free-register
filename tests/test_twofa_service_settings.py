# -*- coding: utf-8 -*-
import unittest

from core import twofa_service


class TwofaServiceSettingsTests(unittest.TestCase):
    def test_executor_uses_configured_worker_count(self):
        settings = twofa_service.queue_settings()

        self.assertGreaterEqual(settings["workers"], 1)
        self.assertLessEqual(settings["workers"], 16)
        self.assertGreaterEqual(settings["queue_limit"], settings["workers"])
        # 实现按 _MAX_WORKERS 建池、用 _RUNNING >= _WORKERS 限流（改配置无需重建线程池）。
        self.assertEqual(twofa_service._EXECUTOR._max_workers, twofa_service._MAX_WORKERS)
        self.assertEqual(settings["workers"], twofa_service._WORKERS)

    def test_integer_settings_are_bounded(self):
        original = getattr(twofa_service._twofa_cfg, "TWOFA_WORKERS", None)
        try:
            twofa_service._twofa_cfg.TWOFA_WORKERS = 999
            self.assertEqual(twofa_service._int_setting("TWOFA_WORKERS", 4, 1, 16), 16)
            twofa_service._twofa_cfg.TWOFA_WORKERS = -5
            self.assertEqual(twofa_service._int_setting("TWOFA_WORKERS", 4, 1, 16), 1)
        finally:
            twofa_service._twofa_cfg.TWOFA_WORKERS = original


if __name__ == "__main__":
    unittest.main()
