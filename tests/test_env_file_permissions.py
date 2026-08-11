import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import env_loader


class EnvFilePermissionTests(unittest.TestCase):
    def _write(self, env_path: Path, updates: dict[str, str]) -> list[str]:
        with patch.object(env_loader, "_ENV_PATH", env_path), patch.object(
            env_loader, "load_env", return_value=env_path
        ):
            return env_loader.write_env_values(updates)

    def test_existing_private_env_stays_private_after_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text('KEEP="old"\n', encoding="utf-8")
            os.chmod(env_path, 0o600)

            with patch("config.env_loader.os.chmod", wraps=os.chmod) as chmod:
                written = self._write(env_path, {"KEEP": "new"})

            self.assertEqual(written, ["KEEP"])
            self.assertEqual(env_path.read_text(encoding="utf-8"), 'KEEP="new"\n')
            chmod.assert_any_call(env_path, 0o600)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_new_env_is_created_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"

            with patch("config.env_loader.os.chmod", wraps=os.chmod) as chmod:
                written = self._write(env_path, {"NEW_KEY": "fixture-value"})

            self.assertEqual(written, ["NEW_KEY"])
            self.assertIn('NEW_KEY="fixture-value"', env_path.read_text(encoding="utf-8"))
            chmod.assert_any_call(env_path, 0o600)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_failed_replace_removes_private_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text('KEEP="old"\n', encoding="utf-8")
            os.chmod(env_path, 0o600)

            with patch.object(env_loader, "_ENV_PATH", env_path), patch.object(
                env_loader, "load_env", return_value=env_path
            ), patch("config.env_loader.os.replace", side_effect=OSError("replace fixture failure")):
                with self.assertRaisesRegex(OSError, "replace fixture failure"):
                    env_loader.write_env_values({"KEEP": "new"})

            self.assertEqual(env_path.read_text(encoding="utf-8"), 'KEEP="old"\n')
            self.assertEqual(list(env_path.parent.glob(".env.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
