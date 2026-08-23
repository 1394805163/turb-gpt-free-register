# -*- coding: utf-8 -*-
import threading
import time
import unittest

from core.browser_task_gate import browser_task_slot


class BrowserTaskGateTests(unittest.TestCase):
    def test_registration_and_oauth_cannot_hold_browser_gate_together(self):
        started = threading.Event()
        acquired = threading.Event()

        def contender():
            started.set()
            with browser_task_slot("oauth", timeout=2):
                acquired.set()

        with browser_task_slot("registration", timeout=0.1):
            worker = threading.Thread(target=contender)
            worker.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            self.assertFalse(acquired.is_set())

        worker.join(1)
        self.assertTrue(acquired.is_set())

    def test_gate_timeout_returns_false_without_raising(self):
        with browser_task_slot("registration", timeout=0.1):
            acquired = []
            with browser_task_slot("oauth", timeout=0.01) as ok:
                acquired.append(ok)
            self.assertEqual(acquired, [False])


if __name__ == "__main__":
    unittest.main()
