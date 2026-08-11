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


STATEFUL_SYSTEMD_FIXTURE = r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/units" "$work/state" "$work/app/.venv/bin" "$work/app/deploy/linux"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/systemd-analyze"
cat > "$work/systemctl" <<'FAKE_SYSTEMCTL'
#!/usr/bin/env bash
set -u
root="$(cd "$(dirname "$0")" && pwd -P)"
state="$root/state"
printf '%s\n' "$*" >> "$state/calls"
case "${1:-}" in
  is-enabled) test -f "$state/enabled" ;;
  is-active) test -f "$state/active" ;;
  daemon-reload) ;;
  enable)
    touch "$state/enabled"
    [[ ! -f "$state/fail-enable" ]] || exit 1
    if [[ " $* " == *" --now "* ]]; then
      if [[ -f "$state/fail-start" ]]; then rm -f "$state/active"; exit 1; fi
      touch "$state/active"
    fi
    ;;
  disable) rm -f "$state/enabled" ;;
  start)
    if [[ -f "$state/fail-start" ]]; then rm -f "$state/active"; exit 1; fi
    touch "$state/active"
    ;;
  restart)
    if [[ -f "$state/fail-restart" ]]; then
      rm -f "$state/fail-restart" "$state/active"
      exit 1
    fi
    touch "$state/active"
    ;;
  stop) rm -f "$state/active" ;;
esac
FAKE_SYSTEMCTL
cat > "$work/id" <<'FAKE_ID'
#!/usr/bin/env bash
case "$*" in
  '-u turbgpt') printf '1001\n' ;;
  '-g turbgpt') printf '1001\n' ;;
  '-gn turbgpt') printf 'turbgpt\n' ;;
  *) exec /usr/bin/id "$@" ;;
esac
FAKE_ID
cat > "$work/getent" <<'FAKE_GETENT'
#!/usr/bin/env bash
[[ "$*" == 'passwd turbgpt' ]] && printf 'turbgpt:x:1001:1001::/home/interactive:/usr/sbin/nologin\n'
FAKE_GETENT
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/useradd"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/app/.venv/bin/gunicorn"
touch "$work/app/.env" "$work/app/deploy/linux/gunicorn.conf.py" \
  "$work/app/requirements.txt" "$work/app/web.py"
chmod +x "$work/systemd-analyze" "$work/systemctl" "$work/id" "$work/getent" \
  "$work/useradd" "$work/app/.venv/bin/gunicorn"
'''


BOOTSTRAP_IDENTITY_FIXTURE = r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/identity"
cat > "$work/identity/id" <<'FAKE_ID'
#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd -P)"
case "$*" in
  '-u') printf '0\n' ;;
  '-un') printf 'root\n' ;;
  '-u turbgpt') [[ -f "$root/user-created" ]] && printf '1001\n' || exit 1 ;;
  '-g turbgpt') [[ -f "$root/user-created" ]] && printf '1001\n' || exit 1 ;;
  '-gn turbgpt') [[ -f "$root/user-created" ]] && printf 'turbgpt\n' || exit 1 ;;
  '-u uid-zero-alias') printf '0\n' ;;
  '-g uid-zero-alias') printf '0\n' ;;
  '-gn uid-zero-alias') printf 'uid-zero-alias\n' ;;
  *) exit 1 ;;
esac
FAKE_ID
cat > "$work/identity/getent" <<'FAKE_GETENT'
#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd -P)"
case "$*" in
  'passwd turbgpt') [[ -f "$root/user-created" ]] && printf 'turbgpt:x:1001:1001::/home/interactive:/usr/sbin/nologin\n' || exit 2 ;;
  'passwd uid-zero-alias') printf 'uid-zero-alias:x:0:0::/root:/bin/sh\n' ;;
  *) exit 2 ;;
esac
FAKE_GETENT
cat > "$work/identity/useradd" <<'FAKE_USERADD'
#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd -P)"
printf '%s\n' "$*" >> "$root/useradd.calls"
touch "$root/user-created"
FAKE_USERADD
chmod +x "$work/identity/id" "$work/identity/getent" "$work/identity/useradd"
'''


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

    def test_pid_fallback_finds_only_exact_gunicorn_master_for_this_app_and_endpoint(self):
        result = run_bash_snippet(
            r'''
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
app="$work/app"
proc="$work/proc"
mkdir -p "$app/.venv/bin" "$app/deploy/linux" "$proc"
cp webui.sh "$app/webui.sh"
chmod +x "$app/webui.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$app/.venv/bin/python"
printf '#!/usr/bin/env bash\ntouch "$(dirname "$0")/../../unexpected-start"\nexit 1\n' > "$app/.venv/bin/gunicorn"
chmod +x "$app/.venv/bin/python" "$app/.venv/bin/gunicorn"
make_proc() {
  pid="$1" host="$2" port="$3" gunicorn="$4" config="$5" spec="$6" ppid="${7:-0}"
  mkdir -p "$proc/$pid"
  printf '%s\0' "$gunicorn" --config "$config" "$spec" > "$proc/$pid/cmdline"
  printf '%s\0' "HOST=$host" "PORT=$port" > "$proc/$pid/environ"
  printf 'Name:\tgunicorn\nPPid:\t%s\n' "$ppid" > "$proc/$pid/status"
}
good_gunicorn="$app/.venv/bin/gunicorn"
good_config="$app/deploy/linux/gunicorn.conf.py"
make_proc 99101 127.0.0.1 5000 "$good_gunicorn" "$good_config" 'webui.app:create_app()'
make_proc 99102 127.0.0.1 5001 "$good_gunicorn" "$good_config" 'webui.app:create_app()'
make_proc 99103 127.0.0.1 5000 "$work/other/.venv/bin/gunicorn" "$good_config" 'webui.app:create_app()'
make_proc 99104 127.0.0.1 5000 "$good_gunicorn" "$work/other/gunicorn.conf.py" 'webui.app:create_app()'
make_proc 99105 127.0.0.1 5000 "$good_gunicorn" "$good_config" 'other.app:create_app()'
make_proc 99106 127.0.0.1 5000 "$good_gunicorn" "$good_config" 'webui.app:create_app()' 99101
mkdir -p "$proc/99107"
printf '%s\0' /bin/echo "$good_gunicorn" --config "$good_config" 'webui.app:create_app()' > "$proc/99107/cmdline"
printf '%s\0' 'HOST=127.0.0.1' 'PORT=5000' > "$proc/99107/environ"
printf 'Name:\techo\nPPid:\t0\n' > "$proc/99107/status"
mkdir -p "$proc/99108"
printf '%s\0' "$app/.venv/bin/python3.12" "$good_gunicorn" --config "$good_config" 'webui.app:create_app()' > "$proc/99108/cmdline"
printf '%s\0' 'HOST=127.0.0.1' 'PORT=5000' > "$proc/99108/environ"
printf 'Name:\tgunicorn\nPPid:\t0\n' > "$proc/99108/status"
set +e
output=$(cd "$app" && PROC_ROOT="$proc" HOST=127.0.0.1 PORT=5000 ./webui.sh status)
status=$?
start_output=$(cd "$app" && PROC_ROOT="$proc" HOST=127.0.0.1 PORT=5000 ./webui.sh start)
start_status=$?
set -e
printf 'status=%s\n%s\nstart_status=%s\n%s\nstarted=%s\n' "$status" "$output" \
  "$start_status" "$start_output" "$(test -e "$app/unexpected-start" && echo yes || echo no)"
'''
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=0", result.stdout)
        self.assertIn("start_status=0", result.stdout)
        self.assertIn("WebUI \u5df2\u5728\u8fd0\u884c", result.stdout)
        self.assertIn("started=no", result.stdout)
        self.assertIn("99101", result.stdout)
        self.assertIn("99108", result.stdout)
        for unrelated_pid in ("99102", "99103", "99104", "99105", "99106", "99107"):
            self.assertNotIn(unrelated_pid, result.stdout)

    def test_pid_discovery_reads_injectable_proc_root_and_exact_environment_entries(self):
        self.assertIn('PROC_ROOT="${PROC_ROOT:-/proc}"', WEBUI)
        self.assertIn('"HOST=$HOST"', WEBUI)
        self.assertIn('"PORT=$PORT"', WEBUI)
        self.assertNotIn("pgrep -f", WEBUI)
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

    def test_service_home_and_private_runtime_directories_are_fixed_and_mode_0700(self):
        bootstrap = read("deploy/linux/bootstrap.sh")
        installer = read("deploy/linux/install-systemd.sh")
        for script in (bootstrap, installer):
            self.assertIn('SERVICE_HOME="/var/lib/turb-gpt-register"', script)
            self.assertIn("--service-home", script)
            self.assertRegex(script, r"install -d -m 0700")
            self.assertRegex(script, r"stat -c ['\"]%a['\"]")
        self.assertNotIn('SERVICE_HOME="$(getent passwd', bootstrap)
        self.assertIn('Environment=__HOME_ENV__', read("deploy/linux/turb-gpt-register.service.template"))

    def test_bootstrap_creates_missing_reserved_user_but_rejects_other_missing_explicit_user(self):
        result = run_bash_snippet(
            BOOTSTRAP_IDENTITY_FIXTURE
            + r'''
set +e
created=$(deploy/linux/bootstrap.sh --identity-test-root "$work/identity" \
  --resolve-service-user-only --service-user turbgpt 2>&1)
created_status=$?
created_useradd=$(cat "$work/useradd.calls")
rm -f "$work/user-created"
: > "$work/useradd.calls"
missing=$(deploy/linux/bootstrap.sh --identity-test-root "$work/identity" \
  --resolve-service-user-only --service-user another-user 2>&1)
missing_status=$?
set -e
printf 'created_status=%s\n%s\ncreated_useradd=%s\nmissing_status=%s\n%s\nuseradd=%s\n' \
  "$created_status" "$created" "$created_useradd" "$missing_status" "$missing" "$(cat "$work/useradd.calls")"
'''
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("created_status=0", result.stdout)
        self.assertIn("user=turbgpt", result.stdout)
        self.assertIn("home=/var/lib/turb-gpt-register", result.stdout)
        self.assertIn("created_useradd=--system --create-home --home-dir /var/lib/turb-gpt-register", result.stdout)
        self.assertIn("missing_status=1", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("useradd="), result.stdout)

    def test_bootstrap_rejects_uid_zero_alias_with_fake_identity_commands(self):
        result = run_bash_snippet(
            BOOTSTRAP_IDENTITY_FIXTURE
            + r'''
set +e
output=$(deploy/linux/bootstrap.sh --identity-test-root "$work/identity" \
  --resolve-service-user-only --service-user uid-zero-alias 2>&1)
status=$?
set -e
printf 'status=%s\n%s\n' "$status" "$output"
'''
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=1", result.stdout)
        self.assertNotIn("未知选项", result.stdout)
        self.assertIn("服务用户 UID 0", result.stdout)

    def test_installer_rejects_uid_zero_alias_with_fake_id_and_getent(self):
        result = run_bash_snippet(
            STATEFUL_SYSTEMD_FIXTURE
            + r'''
cat > "$work/id" <<'FAKE_ID'
#!/usr/bin/env bash
case "$*" in
  '-u uid-zero-alias'|'-g uid-zero-alias') printf '0\n' ;;
  '-gn uid-zero-alias') printf 'uid-zero-alias\n' ;;
  *) exec /usr/bin/id "$@" ;;
esac
FAKE_ID
printf '#!/usr/bin/env bash\nprintf "uid-zero-alias:x:0:0::/root:/bin/sh\\n"\n' > "$work/getent"
chmod +x "$work/id" "$work/getent"
set +e
deploy/linux/install-systemd.sh --test-root "$work" --app-dir "$work/app" \
  --service-user uid-zero-alias --service-group uid-zero-alias --service-home /var/lib/turb-gpt-register 2>"$work/error"
status=$?
set -e
printf 'status=%s\n%s\n' "$status" "$(cat "$work/error")"
'''
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=1", result.stdout)
        self.assertIn("service user UID 0", result.stdout)

    def test_bootstrap_and_doctor_run_full_cloak_doctor_as_service_user(self):
        bootstrap = read("deploy/linux/bootstrap.sh")
        doctor = read("deploy/linux/doctor.sh")
        self.assertRegex(bootstrap, r'run_as_service_user "\$VENV_PYTHON" -m cloakbrowser doctor(?:\s|$)')
        self.assertNotIn("cloakbrowser doctor --quick", bootstrap)
        self.assertRegex(doctor, r'run_as_service_user "\$VENV_PYTHON" -m cloakbrowser doctor(?:\s|$)')
        self.assertNotIn(".venv/bin/cloakbrowser", doctor)
        self.assertIn("--write-out", doctor)
        self.assertIn('[[ "$HTTP_STATUS" == "200" ]]', doctor)

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
                    expected_path = app_dir.replace(" ", r"\x20")
                    self.assertIn("User=turbgpt", unit)
                    self.assertIn("Group=turbgpt", unit)
                    self.assertNotIn('User="turbgpt"', unit)
                    self.assertNotIn('Group="turbgpt"', unit)
                    self.assertIn(f"WorkingDirectory={expected_path}", unit)
                    self.assertIn(f"EnvironmentFile={expected_path}/.env", unit)
                    self.assertNotIn('WorkingDirectory="', unit)
                    self.assertNotIn('EnvironmentFile="', unit)
                    self.assertIn('Environment="HOME=/home/turb gpt"', unit)
                    self.assertIn(
                        f'ExecStart="{app_dir}/.venv/bin/gunicorn" --config '
                        f'"{app_dir}/deploy/linux/gunicorn.conf.py" webui.app:create_app()',
                        unit,
                    )
                    self.assertNotIn("\\x2f", unit)

    def test_render_only_escapes_specifiers_backslashes_and_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = '/opt/100% "quoted"\\path with space'
            service_home = '/home/100% "quoted"\\home with space'
            unit = self.render_unit(
                app_dir,
                Path(temp_dir) / "rendered.service",
                service_home,
            )
        escaped_path = r'/opt/100%%\x20\x22quoted\x22\x5cpath\x20with\x20space'
        self.assertIn(f"WorkingDirectory={escaped_path}", unit)
        self.assertIn(f"EnvironmentFile={escaped_path}/.env", unit)
        self.assertIn(
            r'Environment="HOME=/home/100%% \"quoted\"\\home with space"',
            unit.splitlines(),
        )
        self.assertIn(
            r'ExecStart="/opt/100%% \"quoted\"\\path with space/.venv/bin/gunicorn" '
            r'--config "/opt/100%% \"quoted\"\\path with space/deploy/linux/gunicorn.conf.py" '
            r'webui.app:create_app()',
            unit.splitlines(),
        )
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertNotIn('EnvironmentFile="', unit)
        self.assertNotIn('WorkingDirectory=/opt/100% ', unit)

    def test_render_only_rejects_invalid_identity_scalars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for option, value in (
                ("--service-user", "turb gpt"),
                ("--service-group", 'turb"gpt'),
                ("--service-group", "turb\tgpt"),
            ):
                with self.subTest(option=option, value=value):
                    result = run_bash(
                        "deploy/linux/install-systemd.sh",
                        "--render-only",
                        str(Path(temp_dir) / "rendered.service"),
                        "--app-dir",
                        "/opt/turb-gpt-register",
                        "--service-user",
                        "turbgpt",
                        "--service-group",
                        "turbgpt",
                        "--service-home",
                        "/home/turbgpt",
                        option,
                        value,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_render_only_does_not_reprocess_tokens_inside_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            unit = self.render_unit(
                "/opt/__HOST_ENV__",
                Path(temp_dir) / "rendered.service",
                service_home="/home/__APP_DIR__",
            )
        self.assertIn('WorkingDirectory=/opt/__HOST_ENV__', unit)
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
        "需要 Linux systemd-analyze",
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
        "需要 root 和 runuser 才能验证权限检查",
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
        self.assertNotRegex(script, r"\?{2,}")
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
printf '#!/usr/bin/env bash\necho "$*" >> "$(dirname "$0")/systemctl.log"\nexit 0\n' > "$work/systemctl"
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
printf '#!/usr/bin/env bash\necho "CALL:$1"\nif [ "$1" = daemon-reload ]; then\n  count_file="$0.daemon-count"\n  count=0; test -f "$count_file" && count=$(cat "$count_file")\n  count=$((count + 1)); echo "$count" > "$count_file"\n  [ "$count" -ne 1 ] || exit 1\nfi\nexit 0\n' > "$work/systemctl"
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
            STATEFUL_SYSTEMD_FIXTURE
            + r'''
printf 'old unit\n' > "$work/units/turb-gpt-register.service"
touch "$work/state/fail-enable"
set +e
deploy/linux/install-systemd.sh --test-root "$work" --app-dir "$work/app" \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt
status=$?
set -e
printf 'status=%s\ncontent=%s\nenabled=%s\nactive=%s\n%s\n' "$status" \
  "$(cat "$work/units/turb-gpt-register.service")" \
  "$(test -e "$work/state/enabled" && echo yes || echo no)" \
  "$(test -e "$work/state/active" && echo yes || echo no)" "$(cat "$work/state/calls")"
'''
        )
        self.assertIn("status=1", result.stdout)
        self.assertIn("content=old unit", result.stdout)
        self.assertIn("enabled=no", result.stdout)
        self.assertIn("active=no", result.stdout)
        self.assertEqual(result.stdout.count("daemon-reload"), 2, result.stdout + result.stderr)

    def test_enable_success_then_start_failure_cleans_first_install_state(self):
        result = run_bash_snippet(
            STATEFUL_SYSTEMD_FIXTURE
            + r'''
touch "$work/state/fail-start"
set +e
deploy/linux/install-systemd.sh --test-root "$work" --app-dir "$work/app" \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt 2>"$work/error"
status=$?
set -e
printf 'status=%s\nunit=%s\nenabled=%s\nactive=%s\n--calls--\n%s\n' \
  "$status" "$(test -e "$work/units/turb-gpt-register.service" && echo yes || echo no)" \
  "$(test -e "$work/state/enabled" && echo yes || echo no)" \
  "$(test -e "$work/state/active" && echo yes || echo no)" "$(cat "$work/state/calls")"
'''
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=1", result.stdout)
        self.assertIn("unit=no", result.stdout)
        self.assertIn("enabled=no", result.stdout)
        self.assertIn("active=no", result.stdout)
        calls = result.stdout.split("--calls--", 1)[1]
        self.assertIn("enable turb-gpt-register.service", calls)
        self.assertIn("start turb-gpt-register.service", calls)
        self.assertIn("disable turb-gpt-register.service", calls)

    def test_failed_update_restores_old_enabled_and_active_state(self):
        result = run_bash_snippet(
            STATEFUL_SYSTEMD_FIXTURE
            + r'''
printf 'old unit\n' > "$work/units/turb-gpt-register.service"
touch "$work/state/enabled" "$work/state/active" "$work/state/fail-restart"
set +e
deploy/linux/install-systemd.sh --test-root "$work" --app-dir "$work/app" \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt 2>"$work/error"
status=$?
set -e
rm -f "$work/state/fail-restart"
printf 'status=%s\ncontent=%s\nenabled=%s\nactive=%s\n--calls--\n%s\n' \
  "$status" "$(cat "$work/units/turb-gpt-register.service")" \
  "$(test -e "$work/state/enabled" && echo yes || echo no)" \
  "$(test -e "$work/state/active" && echo yes || echo no)" "$(cat "$work/state/calls")"
'''
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=1", result.stdout)
        self.assertIn("content=old unit", result.stdout)
        self.assertIn("enabled=yes", result.stdout)
        self.assertIn("active=yes", result.stdout)
        calls = result.stdout.split("--calls--", 1)[1]
        self.assertGreaterEqual(calls.count("enable turb-gpt-register.service"), 2)
        self.assertGreaterEqual(calls.count("restart turb-gpt-register.service"), 2)

    def test_success_restarts_active_service_starts_inactive_and_no_start_stays_stopped(self):
        scenarios = (
            ("active", "touch \"$work/state/active\"", "", "restart turb-gpt-register.service"),
            ("inactive", ":", "", "start turb-gpt-register.service"),
            ("no-start", ":", "--no-start", ""),
        )
        for name, setup, option, expected in scenarios:
            with self.subTest(name=name):
                result = run_bash_snippet(
                    STATEFUL_SYSTEMD_FIXTURE
                    + f'''
printf 'old unit\\n' > "$work/units/turb-gpt-register.service"
{setup}
deploy/linux/install-systemd.sh --test-root "$work" --app-dir "$work/app" \\
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt {option}
printf '%s\\n' '--calls--'
cat "$work/state/calls"
'''
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = result.stdout.split("--calls--", 1)[1]
                self.assertNotIn("enable --now", calls)
                if expected:
                    self.assertIn(expected, calls)
                else:
                    self.assertNotIn("start turb-gpt-register.service", calls)
                    self.assertNotIn("restart turb-gpt-register.service", calls)


class LinuxDocumentationAndCITests(unittest.TestCase):
    def test_ci_targets_ubuntu_2404(self):
        workflow = read(".github/workflows/linux-ci.yml")
        for command in (
            "python-version: '3.12'",
            "python -m venv .venv",
            ".venv/bin/python -m pip install -r requirements.txt",
            "bash -n deploy/linux/*.sh webui.sh",
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
        rollback_heading = section.index("\u56de\u6eda\u5230\u4e0a\u4e00\u4e2a\u63d0\u4ea4")
        followup = section.index("\u786e\u8ba4\u95ee\u9898")
        blocks = (section[:rollback_heading], section[rollback_heading:followup])
        for block in blocks:
            stop = block.index("sudo systemctl stop turb-gpt-register.service")
            git = block.index("sudo -u turbgpt -- git -C", stop)
            install = block.index("bootstrap.sh")
            no_start = block.index("--no-start", install)
            start = block.index("sudo systemctl start turb-gpt-register.service")
            doctor = block.index("sudo deploy/linux/doctor.sh")
            self.assertLess(stop, git)
            self.assertLess(git, install)
            self.assertLess(install, no_start)
            self.assertLess(no_start, start)
            self.assertLess(start, doctor)
            self.assertNotIn("systemctl restart", block)

    def test_first_install_bootstraps_without_start_before_env_edit_and_service_start(self):
        docs = read("LINUX_DEPLOY.md")
        section = docs[docs.index("## \u9996\u6b21\u90e8\u7f72"):docs.index("## \u624b\u52a8\u5b89\u88c5")]
        bootstrap = section.index("bootstrap.sh")
        no_start = section.index("--no-start", bootstrap)
        edit = section.index("sudoedit", no_start)
        start = section.index("sudo systemctl start turb-gpt-register.service", edit)
        doctor = section.index("sudo deploy/linux/doctor.sh", start)
        self.assertLess(bootstrap, no_start)
        self.assertLess(no_start, edit)
        self.assertLess(edit, start)
        self.assertLess(start, doctor)
        self.assertIn("/var/lib/turb-gpt-register", section)

    def test_followup_unit_guidance_states_install_restart_doctor_order(self):
        docs = read("LINUX_DEPLOY.md")
        start = docs.index("\u786e\u8ba4\u95ee\u9898")
        end = docs.index("## 2C2G \u6392\u67e5\u6e05\u5355")
        guidance = docs[start:end]
        install = guidance.index("\u91cd\u65b0\u5b89\u88c5 unit")
        restart = guidance.index("sudo systemctl start turb-gpt-register.service")
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
        with self.subTest("HAR 不得被 Git 跟踪"):
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
        with self.subTest("HAR 模式必须被 .gitignore 忽略"):
            self.assertEqual(ignored.returncode, 0, ignored.stderr)


class LinuxGunicornSmokeContractTests(unittest.TestCase):
    def test_ci_runs_real_gunicorn_lifecycle_smoke_without_env_or_cloak_download(self):
        workflow = read(".github/workflows/linux-ci.yml")
        required = (
            ".venv/bin/gunicorn",
            "--config deploy/linux/gunicorn.conf.py",
            "webui.app:create_app()",
            'status="$(curl',
            "--write-out '%{http_code}'",
            'test "$status" = "200"',
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


class Utf8HarNoticeTests(unittest.TestCase):
    def test_round_one_har_notices_are_utf8_without_bom_or_repeated_question_marks(self):
        notice = "HAR \u662f\u672c\u5730\u5ffd\u7565\u8f93\u5165\uff0c\u4ed3\u5e93\u53ea\u4fdd\u7559\u8131\u654f\u5206\u6790\u7ed3\u8bba\u3002"
        for relative_path in (
            ".gitignore",
            "config/browser.py",
            "docs/protocol_fingerprint_har_analysis.md",
            "tests/test_linux_deployment.py",
            "README.md",
        ):
            with self.subTest(relative_path=relative_path):
                data = (ROOT / relative_path).read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                self.assertNotRegex(data.decode("utf-8"), r"\?{2,}")
        for relative_path in (
            ".gitignore",
            "config/browser.py",
            "docs/protocol_fingerprint_har_analysis.md",
        ):
            with self.subTest(notice_path=relative_path):
                self.assertIn(notice, read(relative_path))


if __name__ == "__main__":
    unittest.main()
