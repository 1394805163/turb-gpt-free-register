# -*- coding: utf-8 -*-
"""Cloak driver 导航重试：页面跳转销毁 JS 执行上下文时应等待稳定后重试一次。"""
import unittest

from core.cloakbrowser_driver import CloakSeleniumDriver


_NAV_ERROR = "Page.evaluate_handle: Execution context was destroyed, most likely because of a navigation"


class _FakeHandle:
    def __init__(self, value):
        self._value = value
        self.disposed = False

    def as_element(self):
        return None

    def json_value(self):
        return self._value

    def dispose(self):
        self.disposed = True


class _FlakyPage:
    def __init__(self, *, failures=1, value=None, error=_NAV_ERROR):
        self.failures = failures
        self.value = value if value is not None else {"ok": True}
        self.error = error
        self.handle_calls = 0
        self.evaluate_calls = 0
        self.settle_calls = 0

    def evaluate_handle(self, wrapper, payload):
        self.handle_calls += 1
        if self.handle_calls <= self.failures:
            raise Exception(self.error)
        return _FakeHandle(self.value)

    def evaluate(self, wrapper, payload):
        self.evaluate_calls += 1
        if self.evaluate_calls <= self.failures:
            raise Exception(self.error)
        return self.value

    def wait_for_load_state(self, state, timeout=None):
        self.settle_calls += 1

    def wait_for_timeout(self, ms):
        pass


def _driver(page):
    return CloakSeleniumDriver(browser=None, context=None, page=page)


class CloakNavigationRetryTests(unittest.TestCase):
    def test_sync_script_retries_once_after_navigation_destroyed_context(self):
        page = _FlakyPage(failures=1, value={"url": "https://chatgpt.com/"})
        driver = _driver(page)

        result = driver.execute_script("return {url: location.href};")

        self.assertEqual(result, {"url": "https://chatgpt.com/"})
        self.assertEqual(page.handle_calls, 2)
        self.assertEqual(page.settle_calls, 1)

    def test_async_script_retries_once_after_navigation_destroyed_context(self):
        page = _FlakyPage(failures=1, value={"accessToken": "stub"})
        driver = _driver(page)

        result = driver.execute_async_script("const done = arguments[0]; done({accessToken:'stub'});")

        self.assertEqual(result, {"accessToken": "stub"})
        self.assertEqual(page.evaluate_calls, 2)
        self.assertEqual(page.settle_calls, 1)

    def test_other_errors_are_not_retried(self):
        page = _FlakyPage(failures=1, error="Page.evaluate_handle: Target page, context or browser has been closed")
        driver = _driver(page)

        with self.assertRaises(Exception) as ctx:
            driver.execute_script("return 1;")

        self.assertIn("has been closed", str(ctx.exception))
        self.assertEqual(page.handle_calls, 1)
        self.assertEqual(page.settle_calls, 0)

    def test_persistent_navigation_error_still_raises_after_one_retry(self):
        page = _FlakyPage(failures=2)
        driver = _driver(page)

        with self.assertRaises(Exception) as ctx:
            driver.execute_script("return 1;")

        self.assertIn("Execution context was destroyed", str(ctx.exception))
        self.assertEqual(page.handle_calls, 2)
        self.assertEqual(page.settle_calls, 1)


if __name__ == "__main__":
    unittest.main()
