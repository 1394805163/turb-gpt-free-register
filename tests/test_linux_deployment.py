import pathlib
import runpy
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class GunicornConfigTests(unittest.TestCase):
    def test_single_worker_gthread_runtime(self):
        cfg = runpy.run_path(str(ROOT / "deploy/linux/gunicorn.conf.py"))
        self.assertEqual(cfg["workers"], 1)
        self.assertEqual(cfg["worker_class"], "gthread")
        self.assertEqual(cfg["threads"], 4)
        self.assertFalse(cfg["preload_app"])
        self.assertNotIn("max_requests", cfg)

    def test_runtime_dependency_contains_gunicorn(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8-sig")
        self.assertRegex(requirements, r"(?m)^gunicorn>=")


if __name__ == "__main__":
    unittest.main()
