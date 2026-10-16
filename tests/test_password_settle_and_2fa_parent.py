# -*- coding: utf-8 -*-
"""注册后 2FA/套餐入队归属（父进程）+ 补密码失败收口 / OTP 冷却回归测试。

覆盖 2026-10-17 实测两个缺陷：
1. 注册子进程里入队的 2FA 任务随子进程被杀，DB 卡 totp_setup_status=running
   → 改由父进程在任务成功后统一入队（registration_service._enqueue_post_registration_tasks）。
2. 补密码失败后 UI 仍显示"本地候选密码"（假密码）
   → settle_local_password_after_failure + password_status 标记 / cleared 清除。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core import account_export, account_password, add_password_service, db, registration_service

from tests.test_import_overwrite import _db_storage_patches


def _extra(acc: dict) -> dict:
    raw = acc.get("extra_json")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class _DbCase(unittest.TestCase):
    """每个用例一份临时存储（SQLite 随 _ACCOUNTS_JSON 重定向），绝不落真实库。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._patcher = patch.multiple(db, **_db_storage_patches(Path(self._td.name)))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._td.cleanup)

    def _insert(self, email: str, *, password: str | None = None, **kwargs) -> int:
        acc_id = db.insert_account(
            email=email, access_token=kwargs.pop("access_token", "tok-1"), **kwargs
        )
        if password is not None:
            db.update_account_registration_password(email, password)
        return acc_id

    def _raw(self, email: str) -> dict:
        row = db._load_account_row(email=email)
        assert row is not None, email
        return row


class PasswordSettleTests(_DbCase):
    """settle_local_password_after_failure：三种收口路径 + verified 覆盖。"""

    def test_not_submitted_clears_candidate(self):
        self._insert("settle-a@example.com", password="candidate-pw")
        account_password.settle_local_password_after_failure(
            email="settle-a@example.com",
            prev_password="",
            submitted=False,
            error="TimeoutError: iCloud OTP wait timed out",
            save=True,
        )
        acc = self._raw("settle-a@example.com")
        self.assertFalse(str(acc.get("password") or "").strip())
        self.assertEqual(acc.get("password_status"), "cleared")
        self.assertNotIn("registration_password", _extra(acc))

    def test_submitted_keeps_candidate_as_unverified(self):
        self._insert("settle-b@example.com", password="candidate-pw")
        account_password.settle_local_password_after_failure(
            email="settle-b@example.com",
            prev_password="",
            submitted=True,
            error="connection reset by peer",
            save=True,
        )
        acc = self._raw("settle-b@example.com")
        self.assertEqual(str(acc.get("password") or ""), "candidate-pw")
        self.assertEqual(acc.get("password_status"), "unverified")
        self.assertIn("registration_password", _extra(acc))

    def test_had_password_rolls_back(self):
        self._insert("settle-c@example.com", password="orig-pw")
        db.update_account_registration_password("settle-c@example.com", "candidate-pw")
        account_password.settle_local_password_after_failure(
            email="settle-c@example.com",
            prev_password="orig-pw",
            submitted=False,
            error="HTTP 500",
            save=True,
        )
        acc = self._raw("settle-c@example.com")
        self.assertEqual(str(acc.get("password") or ""), "orig-pw")
        self.assertEqual(_extra(acc).get("registration_password"), "orig-pw")

    def test_verified_overrides_unverified(self):
        self._insert("settle-d@example.com", password="candidate-pw")
        db.mark_account_password_unverified("settle-d@example.com", reason="x")
        db.mark_account_password_verified("settle-d@example.com")
        acc = self._raw("settle-d@example.com")
        self.assertEqual(acc.get("password_status"), "verified")
        self.assertFalse(str(acc.get("password_status_reason") or ""))

    def test_save_false_keeps_candidate(self):
        self._insert("settle-e@example.com", password="candidate-pw")
        account_password.settle_local_password_after_failure(
            email="settle-e@example.com",
            prev_password="",
            submitted=False,
            error="x",
            save=False,
        )
        acc = self._raw("settle-e@example.com")
        self.assertEqual(str(acc.get("password") or ""), "candidate-pw")


class PasswordRetryCooldownTests(_DbCase):
    """OTP 节流 → 递增冷却；sweep 冷却期不重试，到期才挑；终态清冷却。"""

    def test_next_retry_at_only_for_otp_throttle(self):
        otp = add_password_service._next_retry_at("TimeoutError: iCloud OTP wait timed out", 2)
        self.assertTrue(otp)
        delta = datetime.fromisoformat(otp) - datetime.now()
        self.assertGreater(delta.total_seconds(), 15 * 60)
        self.assertLessEqual(delta.total_seconds(), 21 * 60)
        self.assertIsNone(add_password_service._next_retry_at("HTTP 500 server error", 1))

    def _make_pending(self, email: str) -> int:
        acc_id = self._insert(email)
        row = db._load_account_row(acc_id=acc_id)
        row["created_at"] = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        db._save_account_row(row)
        db.set_account_password_pending(email, status="pending", reason="t")
        return acc_id

    def test_sweep_skips_during_cooldown_then_picks_after(self):
        self._make_pending("sweep-a@example.com")
        db.set_account_password_pending(
            "sweep-a@example.com",
            status="pending",
            reason="t",
            next_retry_at=(datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds"),
        )
        queued: list[dict] = []
        with (
            patch.object(add_password_service, "_busy_stages", return_value=[]),
            patch.object(
                add_password_service,
                "enqueue_add_password",
                side_effect=lambda **kw: queued.append(kw) or {"accepted": True},
            ),
        ):
            info = add_password_service.sweep_pending(limit=5, min_age_minutes=15)
            self.assertEqual(info["picked"], 0, msg=info)
            self.assertEqual(queued, [])

            db.set_account_password_pending(
                "sweep-a@example.com",
                status="pending",
                reason="t",
                next_retry_at=(datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"),
            )
            info2 = add_password_service.sweep_pending(limit=5, min_age_minutes=15)

        self.assertEqual(info2["picked"], 1, msg=info2)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["trigger"], "registration_auto_sweep")
        self.assertEqual(self._raw("sweep-a@example.com").get("password_pending_status"), "in_progress")

    def test_sweep_marks_dead_account_skipped(self):
        self._make_pending("sweep-dead@example.com")
        row = db._load_account_row(email="sweep-dead@example.com")
        row["live_check_status"] = "confirmed_dead"
        db._save_account_row(row)
        with (
            patch.object(add_password_service, "_busy_stages", return_value=[]),
            patch.object(add_password_service, "enqueue_add_password") as enqueue,
        ):
            info = add_password_service.sweep_pending(limit=5, min_age_minutes=15)
        self.assertEqual(info["picked"], 0, msg=info)
        enqueue.assert_not_called()
        self.assertEqual(self._raw("sweep-dead@example.com").get("password_pending_status"), "skipped")

    def test_terminal_state_clears_cooldown(self):
        self._insert("sweep-b@example.com", access_token="tok-2")
        future = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
        db.set_account_password_pending("sweep-b@example.com", status="pending", reason="t", next_retry_at=future)
        self.assertTrue(str(self._raw("sweep-b@example.com").get("password_pending_next_retry_at") or ""))
        db.set_account_password_pending("sweep-b@example.com", status="failed", reason="done")
        row = self._raw("sweep-b@example.com")
        self.assertIsNone(row.get("password_pending_next_retry_at"))
        self.assertFalse(row.get("password_pending"))


class PostRegistrationEnqueueTests(_DbCase):
    """注册成功后的 2FA / 套餐查询统一由父进程入队；注册子进程内不入队。"""

    def test_parent_enqueues_2fa_after_registration(self):
        acc_id = self._insert("post-a@example.com")
        calls: list[dict] = []
        with (
            patch("config.twofa.ENABLE_2FA", True),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch(
                "core.twofa_service.enqueue_account_totp_setup",
                side_effect=lambda **kw: calls.append(kw) or {"accepted": True},
            ),
        ):
            registration_service._enqueue_post_registration_tasks(
                account_id=acc_id, email="post-a@example.com"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["account_id"], acc_id)
        self.assertEqual(calls[0]["email"], "post-a@example.com")
        self.assertEqual(calls[0]["trigger"], "registration_auto")

    def test_parent_skips_when_totp_exists(self):
        acc_id = self._insert("post-b@example.com", totp_secret="JBSWY3DPEHPK3PXP")
        with (
            patch("config.twofa.ENABLE_2FA", True),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch("core.twofa_service.enqueue_account_totp_setup") as enqueue,
        ):
            registration_service._enqueue_post_registration_tasks(
                account_id=acc_id, email="post-b@example.com"
            )
        enqueue.assert_not_called()

    def test_parent_respects_disabled_switch(self):
        acc_id = self._insert("post-c@example.com")
        with (
            patch("config.twofa.ENABLE_2FA", False),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch("core.twofa_service.enqueue_account_totp_setup") as enqueue,
        ):
            registration_service._enqueue_post_registration_tasks(
                account_id=acc_id, email="post-c@example.com"
            )
        enqueue.assert_not_called()

    def test_parent_enqueues_plan_check_when_enabled(self):
        acc_id = self._insert("post-d@example.com")
        calls: list[dict] = []
        with (
            patch("config.twofa.ENABLE_2FA", False),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", True),
            patch(
                "core.plan_check_service.enqueue_account_plan_check",
                side_effect=lambda **kw: calls.append(kw) or {"accepted": True},
            ),
        ):
            registration_service._enqueue_post_registration_tasks(
                account_id=acc_id, email="post-d@example.com"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["trigger"], "registration_auto")

    def _save(self, email: str, token: str):
        with patch.object(account_export, "_append_batch_archive", return_value=Path(self._td.name) / "batch"):
            return account_export.save_account_data(email, token, auto_plan_check=False)

    def test_subprocess_save_defers_2fa_to_parent(self):
        calls: list[dict] = []
        with (
            patch.dict(os.environ, {"TURB_REG_SUBPROCESS": "1"}),
            patch("config.twofa.ENABLE_2FA", True),
            patch(
                "core.twofa_service.enqueue_account_totp_setup",
                side_effect=lambda **kw: calls.append(kw) or {"accepted": True},
            ),
        ):
            row_id = self._save("sub-a@example.com", "tok-sub")
        self.assertTrue(row_id)
        self.assertEqual(calls, [])
        self.assertEqual(self._raw("sub-a@example.com").get("password_pending_status"), "pending")

    def test_direct_save_enqueues_2fa(self):
        calls: list[dict] = []
        with (
            patch.dict(os.environ, {"TURB_REG_SUBPROCESS": ""}),
            patch("config.twofa.ENABLE_2FA", True),
            patch(
                "core.twofa_service.enqueue_account_totp_setup",
                side_effect=lambda **kw: calls.append(kw) or {"accepted": True},
            ),
        ):
            row_id = self._save("direct-a@example.com", "tok-direct")
        self.assertTrue(row_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["email"], "direct-a@example.com")


if __name__ == "__main__":
    unittest.main()
