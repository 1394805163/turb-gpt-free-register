# -*- coding: utf-8 -*-
import re
import unittest
from pathlib import Path

from config import cloakbrowser


ROOT = Path(__file__).resolve().parents[1]


class LinuxRuntimeCompatibilityTests(unittest.TestCase):
    def test_cloak_defaults_to_headless_and_uses_runtime_config(self):
        self.assertIs(cloakbrowser.CLOAK_HEADLESS, True)
        config_text = (ROOT / "config" / "cloakbrowser.py").read_text(encoding="utf-8")
        self.assertIn("apply_env_overrides", config_text)
        self.assertNotRegex(config_text, r"[A-Za-z]:\\\\")

    def test_linux_entrypoints_use_posix_virtualenv_and_state_paths(self):
        docs = (ROOT / "LINUX_DEPLOY.md").read_text(encoding="utf-8")
        gunicorn = (ROOT / "deploy" / "linux" / "gunicorn.conf.py").read_text(encoding="utf-8")
        webui = (ROOT / "webui.sh").read_text(encoding="utf-8")
        for text in (docs, gunicorn, webui):
            self.assertNotRegex(text, r"[A-Za-z]:\\\\")
        self.assertIn(".venv/bin/python", docs)
        self.assertIn("workers = 1", gunicorn)
        self.assertIn("worker_class = \"gthread\"", gunicorn)
        self.assertIn("os.environ.get('HOST'", gunicorn)
        self.assertIn('ROOT_DIR/.venv/bin/python', webui)
        self.assertIn("/var/lib/turb-gpt-register", docs)


if __name__ == "__main__":
    unittest.main()
