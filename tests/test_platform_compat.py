# -*- coding: utf-8 -*-
"""不访问真实代理、邮箱或线上注册的跨平台运行时回归测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import web
from core import icloud_mail_pool, registration_service, sentinel_runner


class _FakeProcess:
    def __init__(self, *, remains_alive_after_terminate: bool = True):
        self.pid = 12345
        self._alive = True
        self._remains_alive_after_terminate = remains_alive_after_terminate
        self.calls: list[str] = []

    def terminate(self):
        self.calls.append("terminate")
        if not self._remains_alive_after_terminate:
            self._alive = False

    def kill(self):
        self.calls.append("kill")
        self._alive = False

    def join(self, timeout=None):
        self.calls.append(f"join:{timeout}")

    def is_alive(self):
        return self._alive


class PlatformCompatibilityTests(unittest.TestCase):
    def test_windows_process_cleanup_uses_process_methods_only(self):
        process = _FakeProcess()
        with patch.object(registration_service.os, "name", "nt"), patch.object(
            registration_service.os,
            "getpgid",
            side_effect=AssertionError("Windows must not call getpgid"),
            create=True,
        ), patch.object(
            registration_service.os,
            "getpgrp",
            side_effect=AssertionError("Windows must not call getpgrp"),
            create=True,
        ), patch.object(
            registration_service.os,
            "killpg",
            side_effect=AssertionError("Windows must not call killpg"),
            create=True,
        ):
            registration_service._terminate_registration_process(process, "test-windows")

        self.assertEqual(process.calls.count("terminate"), 1)
        self.assertEqual(process.calls.count("kill"), 1)

    def test_posix_process_cleanup_uses_process_group_when_available(self):
        process = _FakeProcess()
        with patch.object(registration_service.os, "name", "posix"), patch.object(
            registration_service.os, "getpgid", return_value=54321, create=True
        ), patch.object(registration_service.os, "getpgrp", return_value=99999, create=True), patch.object(
            registration_service.os, "killpg", create=True
        ) as killpg, patch.object(registration_service.signal, "SIGKILL", 9, create=True):
            registration_service._terminate_registration_process(process, "test-posix")

        self.assertEqual(process.calls.count("terminate"), 0)
        self.assertEqual(process.calls.count("kill"), 0)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(killpg.call_args_list[0].args, (54321, registration_service.signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (54321, 9))

    def test_single_instance_lock_is_injectable_and_releasable(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "webui.lock"
            first = web._acquire_single_instance(5000, lock_path)
            try:
                with self.assertRaises(RuntimeError):
                    web._acquire_single_instance(5000, lock_path)
            finally:
                web._release_single_instance(first)
                # cleanup is intentionally idempotent for shutdown paths.
                web._release_single_instance(first)

            second = web._acquire_single_instance(5000, lock_path)
            web._release_single_instance(second)

    def test_windows_single_instance_branch_uses_msvcrt(self):
        calls: list[tuple[int, int, int]] = []

        def locking(fd, mode, size):
            calls.append((fd, mode, size))

        fake_msvcrt = types.SimpleNamespace(LK_NBLCK=10, LK_UNLCK=20, locking=locking)
        with tempfile.TemporaryDirectory() as tmp, patch.object(web.os, "name", "nt"), patch.dict(
            sys.modules, {"msvcrt": fake_msvcrt}
        ):
            handle = web._acquire_single_instance(5000, Path(tmp) / "webui.lock")
            web._release_single_instance(handle)

        self.assertEqual([item[1:] for item in calls], [(10, 1), (20, 1)])

    def test_sentinel_runner_closes_and_removes_challenge_file(self):
        token = {"p": "proof", "c": "challenge", "id": "device", "flow": "flow"}
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["cwd"] = kwargs["cwd"]
            challenge_file = Path(command[command.index("--challenge-file") + 1])
            self.assertTrue(challenge_file.is_file())
            self.assertEqual(json.loads(challenge_file.read_text(encoding="utf-8")), {"ok": True})
            return SimpleNamespace(returncode=0, stdout=json.dumps(token), stderr="")

        with patch.object(sentinel_runner.subprocess, "run", side_effect=fake_run):
            result = sentinel_runner.generate_sentinel_token(
                {"ok": True}, "oauth_create_account", "device"
            )

        self.assertEqual(json.loads(result), token)
        command = captured["command"]
        challenge_file = Path(command[command.index("--challenge-file") + 1])
        self.assertFalse(challenge_file.exists())

    def test_mailbox_state_save_does_not_require_posix_owner_apis(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = icloud_mail_pool.ICloudMailboxPool({}, Path(tmp) / "state.json")
            with patch.object(icloud_mail_pool.os, "geteuid", None, create=True), patch.object(
                icloud_mail_pool.os, "chown", None, create=True
            ):
                pool._save({"alias@icloud.com": {"state": "available"}})
            self.assertTrue((Path(tmp) / "state.json").is_file())

    def test_node_executable_name_is_platform_specific(self):
        with patch.object(sentinel_runner.sys, "platform", "win32"):
            self.assertEqual(sentinel_runner._resolve_node_executable(), "node.exe")
        with patch.object(sentinel_runner.sys, "platform", "linux"):
            self.assertEqual(sentinel_runner._resolve_node_executable(), "node")


class SourcePortabilityTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_core_python_does_not_embed_linux_service_paths_or_commands(self):
        forbidden = ("/opt/", "/root/", "/var/", "systemctl", "#!/usr/bin/env bash", ".venv/bin/")
        files = [*self.ROOT.glob("*.py"), *self.ROOT.glob("core/*.py"), *self.ROOT.glob("config/*.py"), *self.ROOT.glob("webui/*.py")]
        violations = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    violations.append(f"{path.relative_to(self.ROOT)}: {needle}")
        self.assertEqual(violations, [])

    def test_python_sources_use_lf_line_endings(self):
        files = [*self.ROOT.glob("*.py"), *self.ROOT.glob("core/*.py"), *self.ROOT.glob("config/*.py"), *self.ROOT.glob("webui/*.py"), *self.ROOT.glob("tests/*.py")]
        crlf = [str(path.relative_to(self.ROOT)) for path in files if b"\r\n" in path.read_bytes()]
        self.assertEqual(crlf, [])

    def test_sensitive_runtime_files_are_not_tracked(self):
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=self.ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        forbidden_names = {
            ".env",
            "用于注册的邮箱.json",
            "注册成功的邮箱.json",
            "注册成功的token.txt",
            "Default-all-domains-1784468371563.json",
        }
        violations = [item for item in tracked if Path(item).name in forbidden_names or "__pycache__" in Path(item).parts]
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
