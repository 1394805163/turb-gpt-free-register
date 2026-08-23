# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import registration_scheduler
from webui import app as webui_app


class RegistrationSchedulerTests(unittest.TestCase):
    def test_schedule_is_persisted_with_daily_repeat_and_hard_worker_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            registration_scheduler, "_STATE_FILE", Path(tmpdir) / "schedule.json"
        ), patch.object(registration_scheduler, "start"):
            result = registration_scheduler.set_schedule(
                run_at="2026-08-24T09:30",
                count=8,
                workers=16,
                repeat="daily",
                email_source="icloud",
            )

            self.assertTrue(result["enabled"])
            self.assertEqual(result["count"], 8)
            self.assertEqual(result["workers"], 2)
            self.assertEqual(result["repeat"], "daily")
            stored = json.loads((Path(tmpdir) / "schedule.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["next_run_at"], "2026-08-24T09:30:00")

    def test_cancel_clears_pending_schedule_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            registration_scheduler, "_STATE_FILE", Path(tmpdir) / "schedule.json"
        ), patch.object(registration_scheduler, "start"):
            registration_scheduler.set_schedule(
                run_at="2026-08-24T09:30",
                count=1,
                workers=1,
                repeat="once",
            )
            result = registration_scheduler.cancel_schedule()

            self.assertFalse(result["enabled"])
            self.assertIsNone(result["next_run_at"])
            self.assertEqual(result["status"], "cancelled")

    def test_webui_can_create_and_cancel_schedule(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            registration_scheduler, "_STATE_FILE", Path(tmpdir) / "schedule.json"
        ), patch.object(registration_scheduler, "start"):
            client = webui_app.create_app(auth_code="test-auth").test_client()
            headers = {"X-Auth-Code": "test-auth"}
            response = client.post(
                "/api/registration/schedule",
                headers=headers,
                json={"run_at": "2026-08-24T09:30", "count": 5, "workers": 2, "repeat": "daily"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["enabled"])

            response = client.post("/api/registration/schedule/cancel", headers=headers, json={})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.get_json()["enabled"])


if __name__ == "__main__":
    unittest.main()
