from pathlib import Path
import re
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEBUI = (ROOT / "webui.sh").read_text(encoding="utf-8")


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class GunicornConfigTests(unittest.TestCase):
    def test_single_worker_gthread_runtime(self):
        cfg = runpy.run_path(str(ROOT / "deploy/linux/gunicorn.conf.py"))
        self.assertEqual(cfg["workers"], 1)
        self.assertEqual(cfg["worker_class"], "gthread")
        self.assertEqual(cfg["threads"], 4)
        self.assertFalse(cfg["preload_app"])
        self.assertEqual(cfg["graceful_timeout"], 45)
        self.assertNotIn("max_requests", cfg)

    def test_runtime_dependency_contains_gunicorn(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8-sig")
        self.assertRegex(requirements, r"(?m)^gunicorn>=")


class WebuiScriptTests(unittest.TestCase):
    def test_gunicorn_command_passes_host_and_port(self):
        match = re.search(r'if \[\[ -x "\$ROOT_DIR/\.venv/bin/gunicorn" \]\]; then(?P<body>.*?)else', WEBUI, re.S)
        self.assertIsNotNone(match)
        self.assertRegex(match.group("body"), r'export HOST PORT|HOST="\$HOST" PORT="\$PORT"')

    def test_pid_fallback_only_matches_legacy_python_entry(self):
        match = re.search(r"find_pids_by_port\(\) \{(?P<body>.*?)\n\}", WEBUI, re.S)
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertNotIn("gunicorn", body)
        self.assertRegex(body, r"python\.\*web\\\.py\.\*--port")
        self.assertIn('pid="$(read_pid)"', WEBUI)


class LinuxDeploymentInstallerTests(unittest.TestCase):
    def test_systemd_memory_and_process_contract(self):
        unit = read("deploy/linux/turb-gpt-register.service.template")
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("MemoryHigh=1700M", unit)
        self.assertNotIn("MemoryMax=", unit)
        self.assertIn("MALLOC_ARENA_MAX=2", unit)
        self.assertIn("OOMPolicy=continue", unit)
        self.assertIn("User=__SERVICE_USER__", unit)
        self.assertIn("Group=__SERVICE_GROUP__", unit)
        self.assertIn("EnvironmentFile=__ENV_FILE__", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("TimeoutStopSec=45", unit)
        self.assertIn("TasksMax=512", unit)

    def test_bootstrap_preserves_secrets_and_installs_cloak_as_service_user(self):
        script = read("deploy/linux/bootstrap.sh")
        self.assertIn('if [[ ! -e "$APP_DIR/.env" ]]', script)
        self.assertIn("cloakbrowser install", script)
        self.assertIn("run_as_service_user", script)
        self.assertNotIn("--no-sandbox", script)

    def test_installer_and_doctor_scripts_exist(self):
        for relative_path in (
            "deploy/linux/install-systemd.sh",
            "deploy/linux/doctor.sh",
        ):
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)


if __name__ == "__main__":
    unittest.main()
