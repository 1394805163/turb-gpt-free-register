import ast
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import account_liveness, codex_retry_service, email_provider
from core.log_safety import email_fingerprint, redact_email


ALIAS = "long-private-alias@icloud.com"


class EmailLogRedactionTests(unittest.TestCase):
    def test_redaction_is_stable_diagnostic_and_never_contains_full_alias(self):
        redacted = redact_email(ALIAS)

        self.assertNotIn(ALIAS, redacted)
        self.assertIn(email_fingerprint(ALIAS), redacted)
        self.assertRegex(redacted, r"^l\*+s@i\*+\.com#[0-9a-f]{10}$")

    def test_email_provider_logs_redacted_alias(self):
        expected_id = email_fingerprint(ALIAS)
        with patch.object(email_provider, "parse_email_sources", return_value=["icloud"]), patch.object(
            email_provider, "_pick_from_source", return_value=ALIAS
        ), self.assertLogs("core.email_provider", level=logging.INFO) as captured:
            self.assertEqual(email_provider.acquire_email(), ALIAS)

        output = "\n".join(captured.output)
        self.assertNotIn(ALIAS, output)
        self.assertIn(expected_id, output)

    def test_liveness_log_name_and_records_do_not_contain_alias(self):
        session = Mock(device_id="device-fixture", proxy=None)
        session.session = Mock()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            account_liveness, "_LOG_DIR", Path(temp_dir)
        ), patch.object(
            account_liveness,
            "_network_preflight_with_retry",
            return_value=(session, "https://auth.openai.com/authorize"),
        ), patch.object(
            account_liveness,
            "follow_authorize",
            return_value="https://auth.openai.com/email-verification",
        ), patch.object(
            account_liveness,
            "_validate_with_retry",
            return_value={"continue_url": "https://chatgpt.com/callback"},
        ), patch.object(account_liveness, "follow_oauth_callback"), patch.object(
            account_liveness,
            "fetch_session",
            return_value={"accessToken": "fixture-token", "user": {}, "account": {}},
        ), self.assertLogs("core.account_liveness", level=logging.INFO) as captured:
            result = account_liveness.check_account_liveness(ALIAS)
            path = account_liveness.log_path(ALIAS)
            file_output = path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertNotIn(ALIAS, path.name)
        self.assertIn(email_fingerprint(ALIAS), path.name)
        output = "\n".join(captured.output) + "\n" + file_output
        self.assertNotIn(ALIAS, output)
        self.assertIn(email_fingerprint(ALIAS), output)

    def test_liveness_error_text_also_redacts_embedded_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            account_liveness, "_LOG_DIR", Path(temp_dir)
        ), patch.object(
            account_liveness,
            "_network_preflight_with_retry",
            side_effect=RuntimeError(f"fixture failure for {ALIAS}"),
        ), self.assertLogs("core.account_liveness", level=logging.INFO) as captured:
            result = account_liveness.check_account_liveness(ALIAS)

        self.assertFalse(result["ok"])
        output = "\n".join(captured.output)
        self.assertNotIn(ALIAS, output)
        self.assertIn(email_fingerprint(ALIAS), output)

    def test_account_log_filenames_use_only_email_fingerprint(self):
        for path in (account_liveness.log_path(ALIAS), codex_retry_service.log_path(ALIAS)):
            with self.subTest(path=path):
                self.assertNotIn(ALIAS, path.name)
                self.assertIn(email_fingerprint(ALIAS), path.name)

    def test_icloud_registration_and_liveness_log_calls_redact_email_arguments(self):
        root = Path(__file__).resolve().parent.parent
        violations: list[str] = []
        for relative in (
            "core/email_provider.py",
            "core/account_liveness.py",
            "core/cloakbrowser_registration.py",
            "core/browser_use_registration.py",
            "core/roxy_registration.py",
            "core/account_export.py",
            "core/chatgpt_auth.py",
            "core/registration_service.py",
            "core/live_check_service.py",
            "core/plan_check_service.py",
            "core/codex_retry_service.py",
            "core/browser_use_codex_oauth.py",
            "core/codex_agent_service.py",
            "core/codex_oauth.py",
            "core/extract_link_service.py",
            "core/roxy_codex_oauth.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
                    continue
                for item in (child for arg in node.args for child in ast.walk(arg) if isinstance(child, ast.Name)):
                    if item.id != "email":
                        continue
                    current = parents.get(item)
                    protected = False
                    while current is not None and current is not node:
                        if (
                            isinstance(current, ast.Call)
                            and isinstance(current.func, ast.Name)
                            and current.func.id == "redact_email"
                        ):
                            protected = True
                            break
                        current = parents.get(current)
                    if not protected:
                        violations.append(f"{relative}:{node.lineno}")
                        break

        self.assertEqual(violations, [], f"完整邮箱日志参数: {violations}")


if __name__ == "__main__":
    unittest.main()
