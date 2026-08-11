import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import db, plan_check_service
from core.account_export import save_account_data
from core.account_liveness import check_account_liveness


class FastLivenessPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_VIEWER_HTML": root / "viewer.html",
        }.items():
            self.stack.enter_context(patch.object(db, name, value))
        self.stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
        self.account_id = db.insert_account(
            email="alias@icloud.com",
            access_token="fixture-access-token",
            plan_type="free",
        )

    def _run_plan_result(self, result: dict, enqueue: Mock) -> dict:
        self.assertTrue(db.claim_account_plan_check(acc_id=self.account_id, trigger="manual"))
        with patch.object(plan_check_service, "check_account_plan", return_value=result), patch.object(
            plan_check_service, "_wait_for_rate_slot", return_value=None
        ), patch.object(plan_check_service, "_QUEUE_SLOTS") as slots, patch(
            "core.chatgpt2api_push.enqueue_account_push", enqueue
        ):
            return plan_check_service._run_plan_check_inner(
                account_id=self.account_id,
                email="alias@icloud.com",
                access_token="fixture-access-token",
                trigger="manual",
                proxy=None,
                timezone_offset_min="-",
            )

    def test_successful_plan_check_marks_token_live_and_enqueues_push(self):
        enqueue = Mock(return_value={"accepted": True})

        result = self._run_plan_result(
            {
                "ok": True,
                "checked_at": "2026-08-11T10:00:00",
                "current_plan_type": "free",
            },
            enqueue,
        )

        self.assertTrue(result["ok"])
        stored = db.get_account(self.account_id)
        self.assertEqual(stored["live_check_status"], "live")
        self.assertEqual(stored["live_check_method"], "token")
        self.assertEqual(stored["access_token"], "fixture-access-token")
        enqueue.assert_called_once_with(self.account_id)

    def test_unauthorized_plan_check_requests_login_refresh_without_marking_dead(self):
        enqueue = Mock(return_value={"accepted": True})

        result = self._run_plan_result(
            {
                "ok": False,
                "checked_at": "2026-08-11T10:00:00",
                "http_status": 401,
                "needs_live_check": True,
                "error": "AT expired",
            },
            enqueue,
        )

        self.assertFalse(result["ok"])
        stored = db.get_account(self.account_id)
        self.assertTrue(stored["needs_live_check"])
        self.assertNotEqual(stored.get("live_check_status"), "confirmed_dead")
        enqueue.assert_not_called()

    def test_registration_save_only_queues_token_plan_check_not_otp_login(self):
        with patch("core.account_export._append_batch_archive", return_value=Path("batch")), patch(
            "core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True}
        ) as plan_enqueue, patch(
            "core.live_check_service.enqueue_account_live_check", return_value={"accepted": True}
        ) as otp_enqueue:
            row_id = save_account_data(
                "new-alias@icloud.com",
                "new-fixture-token",
                extra={"account": {"planType": "free"}},
                email_source="icloud",
            )

        self.assertGreater(row_id, 0)
        plan_enqueue.assert_called_once()
        otp_enqueue.assert_not_called()

    def test_frontend_distinguishes_fast_token_check_from_otp_login(self):
        root = Path(__file__).resolve().parent.parent
        for relative in ("webui/templates/index.html", "webui/templates/index_legacy.html"):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertIn("快速测活/查套餐", text)
            self.assertIn("登录测活/刷新 Token", text)
            self.assertIn("无需邮箱 OTP", text)
            self.assertIn("需要邮箱 OTP", text)

    def test_successful_full_login_liveness_is_recorded_as_otp_method(self):
        session = Mock(device_id="device-fixture", proxy=None)
        session.session = Mock()
        with patch("core.account_liveness._LOG_DIR", Path(self.tmp.name) / "logs"), patch(
            "core.account_liveness._network_preflight_with_retry",
            return_value=(session, "https://auth.openai.com/authorize"),
        ), patch(
            "core.account_liveness.follow_authorize",
            return_value="https://auth.openai.com/email-verification",
        ), patch(
            "core.account_liveness._validate_with_retry",
            return_value={"continue_url": "https://chatgpt.com/callback"},
        ), patch("core.account_liveness.follow_oauth_callback"), patch(
            "core.account_liveness.fetch_session",
            return_value={"accessToken": "refreshed-fixture-token", "user": {}, "account": {}},
        ):
            result = check_account_liveness("alias@icloud.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "otp")


if __name__ == "__main__":
    unittest.main()
