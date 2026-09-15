# -*- coding: utf-8 -*-
"""OTP 输入框校验：逐字符输入丢字符时必须能发现并用原生 setter 重填。"""
import unittest
from unittest.mock import patch


class _FakeOtpDriver:
    """读脚本（无参数）与写脚本（带参数）分别按队列返回值。"""

    def __init__(self, reads, writes=None):
        self.reads = list(reads)
        self.writes = list(writes or [])

    def execute_script(self, script, *args):
        if args:
            return self.writes.pop(0) if self.writes else None
        return self.reads.pop(0) if self.reads else None


class OtpInputValueTests(unittest.TestCase):
    def setUp(self):
        from core import roxy_registration as rr

        self.rr = rr

    def test_value_already_correct_is_not_rewritten(self):
        driver = _FakeOtpDriver(reads=["123456"])
        with patch.object(self.rr.time, "sleep", return_value=None):
            self.assertTrue(self.rr._ensure_otp_input_value(driver, "123456"))
        self.assertFalse([c for c in driver.reads], "不应再读输入框")

    def test_missing_characters_fall_back_to_native_setter(self):
        driver = _FakeOtpDriver(reads=["12345", "123456"], writes=["123456"])
        with patch.object(self.rr.time, "sleep", return_value=None):
            self.assertTrue(self.rr._ensure_otp_input_value(driver, "123456"))
        self.assertFalse(driver.writes, "setter 结果应被消费")

    def test_persistent_mismatch_returns_false(self):
        driver = _FakeOtpDriver(reads=["", "", ""], writes=["", "", ""])
        with patch.object(self.rr.time, "sleep", return_value=None):
            self.assertFalse(self.rr._ensure_otp_input_value(driver, "123456"))

    def test_empty_code_is_rejected(self):
        driver = _FakeOtpDriver(reads=[""])
        self.assertFalse(self.rr._ensure_otp_input_value(driver, ""))


if __name__ == "__main__":
    unittest.main()