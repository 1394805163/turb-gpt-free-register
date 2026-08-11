from pathlib import Path
import os
import re
import runpy
import shutil
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEBUI = (ROOT / "webui.sh").read_text(encoding="utf-8")
BASH = Path("C:/Program Files/Git/bin/bash.exe")
if not BASH.is_file():
    BASH = Path(shutil.which("bash") or "")


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def run_bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BASH), "-c", f"exec {shlex.join(args)}"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env={**os.environ, "MSYS2_ARG_CONV_EXCL": "*", **(env or {})},
    )


def run_bash_snippet(snippet: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BASH), "-c", snippet],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env={**os.environ, "MSYS2_ARG_CONV_EXCL": "*"},
    )


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

    def render_unit(self, app_dir: str, output: Path, service_home: str = "/home/turb gpt") -> str:
        result = run_bash(
            "deploy/linux/install-systemd.sh",
            "--render-only",
            str(output),
            "--app-dir",
            app_dir,
            "--service-user",
            "turbgpt",
            "--service-group",
            "turbgpt",
            "--service-home",
            service_home,
            "--host",
            "[::1]",
            "--port",
            "5001",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_text(encoding="utf-8")

    def test_render_only_keeps_real_systemd_paths_and_exec_argv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for app_dir in ("/opt/turb-gpt-register", "/opt/app with space"):
                with self.subTest(app_dir=app_dir):
                    unit = self.render_unit(app_dir, Path(temp_dir) / "rendered.service")
                    self.assertIn(f'WorkingDirectory="{app_dir}"', unit)
                    self.assertIn(f'EnvironmentFile="{app_dir}/.env"', unit)
                    self.assertIn('Environment="HOME=/home/turb gpt"', unit)
                    self.assertIn(
                        f'ExecStart="{app_dir}/.venv/bin/gunicorn" --config '
                        f'"{app_dir}/deploy/linux/gunicorn.conf.py" webui.app:create_app()',
                        unit,
                    )
                    self.assertNotIn("\\x2f", unit)
                    self.assertNotIn("\\x20", unit)

    def test_render_only_escapes_specifiers_backslashes_and_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = '/opt/100% "quoted"\\path with space'
            unit = self.render_unit(app_dir, Path(temp_dir) / "rendered.service")
        self.assertIn('WorkingDirectory="/opt/100%% \\"quoted\\"\\\\path with space"', unit)
        self.assertIn('EnvironmentFile="/opt/100%% \\"quoted\\"\\\\path with space/.env"', unit)
        self.assertNotIn('WorkingDirectory="/opt/100% ', unit)

    def test_render_only_does_not_reprocess_tokens_inside_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            unit = self.render_unit(
                "/opt/__HOST_ENV__",
                Path(temp_dir) / "rendered.service",
                service_home="/home/__APP_DIR__",
            )
        self.assertIn('WorkingDirectory="/opt/__HOST_ENV__"', unit)
        self.assertIn('Environment="HOME=/home/__APP_DIR__"', unit)

    def test_render_only_rejects_newline_project_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_bash(
                "deploy/linux/install-systemd.sh",
                "--render-only",
                str(Path(temp_dir) / "rendered.service"),
                "--app-dir",
                "/opt/bad\npath",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newline", result.stderr)

    @unittest.skipUnless(
        os.name != "nt" and shutil.which("systemd-analyze"),
        "?? Linux systemd-analyze",
    )
    def test_rendered_unit_passes_systemd_analyze_verify_when_available(self):
        with tempfile.TemporaryDirectory(prefix="turb gpt ") as temp_dir:
            app_dir = Path(temp_dir) / "app with space"
            gunicorn = app_dir / ".venv/bin/gunicorn"
            config = app_dir / "deploy/linux/gunicorn.conf.py"
            gunicorn.parent.mkdir(parents=True)
            config.parent.mkdir(parents=True)
            gunicorn.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gunicorn.chmod(0o755)
            config.write_text("# test config\n", encoding="utf-8")
            (app_dir / ".env").write_text("", encoding="utf-8")
            unit_path = Path(temp_dir) / "turb-gpt-register.service"
            self.render_unit(str(app_dir), unit_path)
            result = subprocess.run(
                ["systemd-analyze", "verify", str(unit_path)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(
        os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("runuser"),
        "?? root ? runuser ???????????",
    )
    def test_access_check_rejects_project_untraversable_by_service_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / "private-app"
            app_dir.mkdir()
            app_dir.chmod(0o700)
            result = run_bash(
                "deploy/linux/install-systemd.sh",
                "--check-access-only",
                "--app-dir",
                str(app_dir),
                "--service-user",
                "nobody",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("service user cannot", result.stderr)

    def test_doctor_rejects_invalid_port_and_newline_host_as_usage_errors(self):
        for args in (("--port", "0"), ("--port", "65536"), ("--port", "x"), ("--host", "bad\nhost")):
            with self.subTest(args=args):
                result = run_bash("deploy/linux/doctor.sh", *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("ERROR:", result.stderr)

    def test_doctor_rejects_non_endpoint_host_syntax(self):
        invalid_hosts = (
            "host name",
            "http://host",
            "host/path",
            "host\tname",
            "[not:ipv6]",
            "a.-b.example",
            "a.b-.example",
        )
        for host in invalid_hosts:
            with self.subTest(host=host):
                result = run_bash("deploy/linux/doctor.sh", "--host", host)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("ERROR:", result.stderr)

    def test_bootstrap_keeps_utf8_chinese_messages(self):
        script = read("deploy/linux/bootstrap.sh")
        self.assertNotIn("??", script)
        self.assertIn("\u7528\u6cd5: sudo deploy/linux/bootstrap.sh", script)
        self.assertIn("Ubuntu \u539f\u751f\u90e8\u7f72\u5b8c\u6210", script)

    def test_bootstrap_service_user_prefers_explicit_then_sudo_user(self):
        explicit = run_bash(
            "deploy/linux/bootstrap.sh",
            "--print-service-user",
            "--service-user",
            "explicit-user",
            env={"SUDO_USER": "sudo-user"},
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(explicit.stdout.strip(), "explicit-user")
        sudo_user = run_bash(
            "deploy/linux/bootstrap.sh",
            "--print-service-user",
            env={"SUDO_USER": "sudo-user"},
        )
        self.assertEqual(sudo_user.returncode, 0, sudo_user.stderr)
        self.assertEqual(sudo_user.stdout.strip(), "sudo-user")

    def test_unit_verify_failure_keeps_existing_unit_unchanged(self):
        result = run_bash_snippet(
            r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/units" "$work/app/.venv/bin" "$work/app/deploy/linux"
printf 'old unit\n' > "$work/units/turb-gpt-register.service"
printf '#!/usr/bin/env bash\nexit 1\n' > "$work/systemd-analyze"
printf '#!/usr/bin/env bash\necho systemctl >> "$1.log"\nexit 0\n' > "$work/systemctl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/app/.venv/bin/gunicorn"
touch "$work/app/.env" "$work/app/deploy/linux/gunicorn.conf.py" "$work/app/requirements.txt" "$work/app/web.py"
chmod +x "$work/systemd-analyze" "$work/systemctl" "$work/app/.venv/bin/gunicorn"
set +e
deploy/linux/install-systemd.sh --apply-unit-only --test-root "$work" --app-dir "$work/app" \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt
status=$?
set -e
printf 'status=%s\ncontent=%s\n' "$status" "$(cat "$work/units/turb-gpt-register.service")"
'''
        )
        self.assertIn("status=1", result.stdout)
        self.assertIn("content=old unit", result.stdout)

    def test_unit_reload_failure_restores_existing_unit_and_reloads_again(self):
        result = run_bash_snippet(
            r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/units" "$work/app/.venv/bin" "$work/app/deploy/linux"
printf 'old unit\n' > "$work/units/turb-gpt-register.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/systemd-analyze"
printf '#!/usr/bin/env bash\ncount_file="$0.count"\ncount=0; test -f "$count_file" && count=$(cat "$count_file")\ncount=$((count + 1)); echo "$count" > "$count_file"\necho "CALL:$1"\nif [ "$1" = daemon-reload ] && [ "$count" -eq 1 ]; then exit 1; fi\nexit 0\n' > "$work/systemctl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/app/.venv/bin/gunicorn"
touch "$work/app/.env" "$work/app/deploy/linux/gunicorn.conf.py" "$work/app/requirements.txt" "$work/app/web.py"
chmod +x "$work/systemd-analyze" "$work/systemctl" "$work/app/.venv/bin/gunicorn"
set +e
deploy/linux/install-systemd.sh --apply-unit-only --test-root "$work" --app-dir "$work/app" \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt
status=$?
set -e
printf 'status=%s\ncontent=%s\n' "$status" "$(cat "$work/units/turb-gpt-register.service")"
'''
        )
        self.assertIn("status=1", result.stdout)
        self.assertIn("content=old unit", result.stdout)
        self.assertEqual(result.stdout.count("CALL:daemon-reload"), 2, result.stdout + result.stderr)

    def test_production_mode_rejects_environment_injected_unit_tools(self):
        result = run_bash_snippet(
            r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/units"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/systemctl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/analyze"
chmod +x "$work/systemctl" "$work/analyze"
set +e
UNIT_DIR="$work/units" SYSTEMCTL_BIN="$work/systemctl" SYSTEMD_ANALYZE_BIN="$work/analyze" \
  deploy/linux/install-systemd.sh --apply-unit-only --app-dir /opt/app \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt
status=$?
set -e
printf 'status=%s\n' "$status"
'''
        )
        self.assertIn("status=2", result.stdout)

    def test_enable_failure_restores_existing_unit_and_reloads_again(self):
        result = run_bash_snippet(
            r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/units" "$work/app/.venv/bin" "$work/app/deploy/linux"
printf 'old unit\n' > "$work/units/turb-gpt-register.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/systemd-analyze"
printf '#!/usr/bin/env bash\necho "CALL:$1"\nif [ "$1" = enable ]; then exit 1; fi\nexit 0\n' > "$work/systemctl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/app/.venv/bin/gunicorn"
touch "$work/app/.env" "$work/app/deploy/linux/gunicorn.conf.py" "$work/app/requirements.txt" "$work/app/web.py"
chmod +x "$work/systemd-analyze" "$work/systemctl" "$work/app/.venv/bin/gunicorn"
set +e
deploy/linux/install-systemd.sh --test-root "$work" --app-dir "$work/app" \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt
status=$?
set -e
printf 'status=%s\ncontent=%s\n' "$status" "$(cat "$work/units/turb-gpt-register.service")"
'''
        )
        self.assertIn("status=1", result.stdout)
        self.assertIn("content=old unit", result.stdout)
        self.assertEqual(result.stdout.count("CALL:daemon-reload"), 2, result.stdout + result.stderr)


class LinuxDocumentationAndCITests(unittest.TestCase):
    def test_ci_targets_ubuntu_2404(self):
        workflow = read(".github/workflows/linux-ci.yml")
        for command in (
            "python-version: '3.12'",
            "python -m venv .venv",
            ".venv/bin/python -m pip install -r requirements.txt",
            "bash -n deploy/linux/*.sh",
            "python -m unittest tests.test_linux_deployment -v",
            "python -m compileall -q .",
            "python -m unittest discover -s tests -v",
        ):
            self.assertIn(command, workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("cloakbrowser install", workflow)
        self.assertNotIn("secrets", workflow)

    def test_linux_docs_cover_two_gib_operations(self):
        docs = read("LINUX_DEPLOY.md")
        for phrase in ("2 核 2 GB", "1 个 Gunicorn worker", "并发上限为 2", "swap", "journalctl"):
            self.assertIn(phrase, docs)

    def test_upgrade_and_rollback_reinstall_unit_before_restart_and_doctor(self):
        docs = read("LINUX_DEPLOY.md")
        section = docs[docs.index("## \u5347\u7ea7\u4e0e\u56de\u6eda"):docs.index("## 2C2G \u6392\u67e5\u6e05\u5355")]
        blocks = section.split("git checkout --detach HEAD^")
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            install = block.index("bootstrap.sh")
            no_start = block.index("--no-start", install)
            restart = block.index("sudo systemctl restart turb-gpt-register.service")
            doctor = block.index("sudo deploy/linux/doctor.sh")
            self.assertLess(install, no_start)
            self.assertLess(no_start, restart)
            self.assertLess(restart, doctor)

    def test_followup_unit_guidance_states_install_restart_doctor_order(self):
        docs = read("LINUX_DEPLOY.md")
        start = docs.index("\u786e\u8ba4\u95ee\u9898")
        end = docs.index("## 2C2G \u6392\u67e5\u6e05\u5355")
        guidance = docs[start:end]
        install = guidance.index("\u91cd\u65b0\u5b89\u88c5 unit")
        restart = guidance.index("sudo systemctl restart turb-gpt-register.service")
        doctor = guidance.index("sudo deploy/linux/doctor.sh")
        self.assertLess(install, restart)
        self.assertLess(restart, doctor)


class RepositoryHygieneTests(unittest.TestCase):
    def test_har_capture_is_untracked_and_ignored(self):
        tracked = subprocess.run(
            ["git", "ls-files", "--", "Default-all-domains-*.json"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        with self.subTest("HAR ??????? Git ??"):
            self.assertEqual(tracked.stdout.splitlines(), [])
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "Default-all-domains-regression.json"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        with self.subTest("HAR ??????? .gitignore ??"):
            self.assertEqual(ignored.returncode, 0, ignored.stderr)


class LinuxGunicornSmokeContractTests(unittest.TestCase):
    def test_ci_runs_real_gunicorn_lifecycle_smoke_without_env_or_cloak_download(self):
        workflow = read(".github/workflows/linux-ci.yml")
        required = (
            ".venv/bin/gunicorn",
            "--config deploy/linux/gunicorn.conf.py",
            "webui.app:create_app()",
            "curl --fail",
            'master_pid=$!',
            'worker_pids="$(pgrep -P "$master_pid")"',
            'kill -TERM "$master_pid"',
            'wait "$master_pid"',
            'kill -0 "$master_pid"',
            'kill -0 "$worker_pid"',
            "trap cleanup EXIT",
            "test ! -e .env",
        )
        for command in required:
            with self.subTest(command=command):
                self.assertIn(command, workflow)
        self.assertNotIn("cloakbrowser install", workflow)
        self.assertNotIn("playwright install", workflow)


if __name__ == "__main__":
    unittest.main()
