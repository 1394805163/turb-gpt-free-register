# -*- coding: utf-8 -*-
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config import proxy as proxy_config
from core import pipeline_concurrency
from core import chatgpt2api_push
from core import codex_agent_service
from core import codex_retry_service
from core import extract_link_service
from core import plan_check_service
from core import registration_service
from core import live_check_service
from webui import config_editor


class PipelineConcurrencyTests(unittest.TestCase):
    def test_default_and_hard_pipeline_concurrency_limits(self):
        self.assertEqual(pipeline_concurrency.PIPELINE_MAX_CONCURRENCY, 2)
        self.assertEqual(registration_service._DEFAULT_MAX_WORKERS, 1)
        self.assertEqual(registration_service._MAX_MAX_WORKERS, 2)
        self.assertEqual(registration_service._normalize_workers(99), 2)
        self.assertEqual(live_check_service._WORKERS, 2)
        self.assertEqual(chatgpt2api_push.queue_settings()["workers"], 2)
        self.assertEqual(proxy_config.PLAN_CHECK_WORKERS, 1)
        self.assertEqual(plan_check_service._WORKERS, 1)
        self.assertEqual(codex_agent_service._WORKERS, 1)
        self.assertEqual(extract_link_service._WORKERS, 2)

        source = Path(proxy_config.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            config_editor._parse_value_from_source(source, "PLAN_CHECK_WORKERS", "int"),
            2,
        )

    def test_shared_slots_limit_mixed_pipeline_stages_to_two(self):
        lock = threading.Lock()
        active = 0
        peak = 0

        def work(stage):
            nonlocal active, peak
            with pipeline_concurrency.pipeline_slot(stage):
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.03)
                with lock:
                    active -= 1

        stages = ["registration", "live_check", "push"] * 3
        with ThreadPoolExecutor(max_workers=9) as executor:
            list(executor.map(work, stages))

        self.assertEqual(peak, 2)
        self.assertEqual(pipeline_concurrency.pipeline_snapshot()["active"], 0)

    def test_extract_link_and_codex_retry_also_use_shared_pipeline_slots(self):
        observed = {}

        def mark_extract_running(_account_id):
            observed["extract_link"] = pipeline_concurrency.pipeline_snapshot()["active"]
            return False

        extract_link_service._QUEUE_SLOTS.acquire()
        with patch.object(
            extract_link_service.db,
            "mark_account_extract_running",
            side_effect=mark_extract_running,
        ):
            extract_link_service._run_extract(
                account_id=1,
                email="extract@example.com",
                access_token="TOKEN",
                link_type="pix",
                cdk="CDK",
                trigger="concurrency_test",
            )

        def fake_codex_oauth(_email, force=False):
            observed["codex_retry"] = pipeline_concurrency.pipeline_snapshot()["active"]
            return {
                "ok": True,
                "status": "success",
                "file_path": "result.json",
                "callback_url": "http://127.0.0.1/callback",
            }

        with TemporaryDirectory() as tmpdir, patch.object(
            codex_retry_service.db,
            "update_account_codex_status",
        ), patch(
            "core.codex_oauth.run_codex_oauth",
            side_effect=fake_codex_oauth,
        ):
            codex_retry_service.run_worker(
                "retry@example.com",
                target_log_path=Path(tmpdir) / "retry.log",
            )

        self.assertEqual(observed["extract_link"], 1)
        self.assertEqual(observed["codex_retry"], 1)
        self.assertEqual(pipeline_concurrency.pipeline_snapshot()["active"], 0)

    def test_registration_plan_check_liveness_and_push_share_the_same_two_slots(self):
        lock = threading.Lock()
        active = 0
        peak = 0

        def tracked_work():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

        def plan_check_inner(**_kwargs):
            tracked_work()
            return {"ok": True}

        def codex_agent_inner(**_kwargs):
            tracked_work()
            return {"ok": True}

        def pipeline_work(stage):
            with pipeline_concurrency.pipeline_slot(stage):
                tracked_work()

        plan_kwargs = {
            "account_id": 1,
            "email": "peak@example.com",
            "access_token": "TOKEN",
            "trigger": "registration_auto",
            "proxy": "http://HOST:PORT",
            "timezone_offset_min": "-",
        }
        codex_agent_kwargs = {
            "account_id": 1,
            "email": "peak@example.com",
            "access_token": "TOKEN",
            "trigger": "registration_auto",
            "verify_task": False,
        }
        with patch.object(
            plan_check_service, "_run_plan_check_inner", side_effect=plan_check_inner
        ), patch.object(
            codex_agent_service, "_run_generate_inner", side_effect=codex_agent_inner
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(pipeline_work, stage)
                    for stage in ("registration", "live_check", "push")
                ]
                futures.extend(
                    executor.submit(plan_check_service._run_plan_check, **plan_kwargs)
                    for _ in range(3)
                )
                futures.extend(
                    executor.submit(codex_agent_service._run_generate, **codex_agent_kwargs)
                    for _ in range(3)
                )
                for future in futures:
                    future.result()

        self.assertEqual(peak, 2)
        self.assertEqual(pipeline_concurrency.pipeline_snapshot()["active"], 0)

    def test_actual_registration_liveness_and_push_wrappers_never_exceed_two(self):
        lock = threading.Lock()
        active = 0
        peak = 0

        def tracked_result(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return {"ok": True, "success": True}

        def run_registration(index):
            return registration_service._run_one_job(index, f"registration-{index}.log")

        def run_liveness(index):
            return live_check_service._run_live_check(
                account_id=index,
                email=f"liveness-{index}@example.com",
                proxy="http://HOST:PORT",
                trigger="peak_test",
            )

        def run_push(index):
            chatgpt2api_push._QUEUE_SLOTS.acquire()
            return chatgpt2api_push._run_queued_push(index)

        with patch.object(
            registration_service.db,
            "get_job",
            return_value={"status": "queued"},
        ), patch.object(
            registration_service,
            "_run_one_job_inner",
            side_effect=tracked_result,
        ), patch.object(
            live_check_service,
            "_run_live_check_inner",
            side_effect=tracked_result,
        ), patch.object(
            chatgpt2api_push,
            "push_account",
            side_effect=tracked_result,
        ):
            with ThreadPoolExecutor(max_workers=9) as executor:
                futures = []
                for index in range(3):
                    futures.append(executor.submit(run_registration, index))
                    futures.append(executor.submit(run_liveness, index + 100))
                    futures.append(executor.submit(run_push, index + 200))
                for future in futures:
                    future.result()

        self.assertEqual(peak, 2)
        self.assertEqual(pipeline_concurrency.pipeline_snapshot()["active"], 0)


if __name__ == "__main__":
    unittest.main()
