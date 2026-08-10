# -*- coding: utf-8 -*-
import unittest

from core.cloakbrowser_driver import CloakElement


class _Keyboard:
    def __init__(self, state):
        self.state = state

    def type(self, text, delay=0):
        text = str(text)
        if self.state.pop("selected_all", False):
            self.state["value"] = text
            self.state["caret"] = len(text)
            return
        caret = int(self.state.get("caret", len(self.state["value"])))
        self.state["value"] = self.state["value"][:caret] + text + self.state["value"][caret:]
        self.state["caret"] = caret + len(text)

    def press(self, key):
        if key in {"Meta+A", "Control+A"}:
            self.state["selected_all"] = True
        elif key == "Backspace":
            if self.state.pop("selected_all", False):
                self.state["value"] = ""
            else:
                self.state["value"] = self.state["value"][:-1]


class _Page:
    def __init__(self, state):
        self.keyboard = _Keyboard(state)


class _Locator:
    def __init__(self, state):
        self.state = state

    def click(self, timeout=0):
        self.state["focused"] = True
        # 浏览器中反复鼠标点击输入框会重新定位光标；这正是分段输入乱序的来源。
        self.state["caret"] = 0

    def focus(self, timeout=0):
        self.state["focused"] = True

    def press_sequentially(self, text, delay=0):
        _Keyboard(self.state).type(text, delay=delay)

    def fill(self, text, timeout=0):
        self.state["value"] = str(text)


class CloakElementTypingTests(unittest.TestCase):
    def test_repeated_send_keys_appends_instead_of_replacing_previous_text(self):
        state = {"value": ""}
        element = CloakElement(_Page(state), locator=_Locator(state))

        for chunk in ("alias", "@", "icloud", ".", "com"):
            element.send_keys(chunk)

        self.assertEqual(state["value"], "alias@icloud.com")

    def test_control_a_then_backspace_clears_existing_value(self):
        from selenium.webdriver.common.keys import Keys

        state = {"value": "stale@example.com"}
        element = CloakElement(_Page(state), locator=_Locator(state))

        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)

        self.assertEqual(state["value"], "")


if __name__ == "__main__":
    unittest.main()
