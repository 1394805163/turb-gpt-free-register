import ast
import re
import unittest
from pathlib import Path


class OtpFlowSafetyTests(unittest.TestCase):
    def test_all_registration_drivers_share_dedupe_and_total_wait_session(self):
        root = Path(__file__).resolve().parent.parent
        for relative in (
            "core/cloakbrowser_registration.py",
            "core/roxy_registration.py",
            "core/browser_use_registration.py",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertIn("OtpWaitSession", text, relative)
            self.assertIn("otp_wait_session.mark_used", text, relative)
        roxy = (root / "core/roxy_registration.py").read_text(encoding="utf-8")
        self.assertNotIn("wait_for_otp(email, after_ts=0.0", roxy)

    def test_logger_calls_never_persist_otp_or_sms_code_variables(self):
        root = Path(__file__).resolve().parent.parent
        violations = []
        explicit_secret_names = {
            "otp",
            "otp_code",
            "email_otp",
            "current_otp",
            "fallback_otp",
            "sms_code",
            "best_otp",
            "totp_code",
            "access_token",
            "refresh_token",
            "secret",
        }
        for path in (root / "core").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
                    continue
                call_source = ast.get_source_segment(source, node) or ""
                unsafe_names = set()
                for item in (child for arg in node.args for child in ast.walk(arg) if isinstance(child, ast.Name)):
                    is_generic_otp_code = (
                        item.id == "code"
                        and any(marker in call_source for marker in ("OTP", "验证码", "重认证", "TOTP"))
                    )
                    if item.id not in explicit_secret_names and not is_generic_otp_code:
                        continue
                    current = parents.get(item)
                    protected = False
                    while current is not None and current is not node:
                        if isinstance(current, ast.Call) and isinstance(current.func, ast.Name) and current.func.id in {
                            "len", "bool", "token_fingerprint", "_token_fingerprint", "_fingerprint"
                        }:
                            protected = True
                            break
                        current = parents.get(current)
                    if not protected:
                        unsafe_names.add(item.id)

                unsafe_preview = bool(re.search(
                    r"\.get\(['\"]subject['\"]\)|\b(?:best_)?subject\b|"
                    r"\b(?:resp|response)\.text\b|json\.dumps\(data",
                    call_source,
                ))
                if unsafe_names or unsafe_preview:
                    violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual(violations, [], f"OTP/SMS 明文日志调用: {violations}")


if __name__ == "__main__":
    unittest.main()
