# -*- coding: utf-8 -*-
"""可恢复的注册定时计划。计划只负责入队，浏览器互斥由任务闸门负责。"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _PROJECT_ROOT / "data" / "registration_schedule.json"
_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _parse_run_at(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1]
    if len(text) == 10 and text[4] == "-":
        text += "T00:00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _load() -> dict[str, Any]:
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(payload: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = _STATE_FILE.with_suffix(_STATE_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(_STATE_FILE)


def get_schedule() -> dict[str, Any]:
    with _LOCK:
        state = _load()
        return {
            "enabled": bool(state.get("enabled", False)),
            "status": str(state.get("status") or "idle"),
            "run_at": state.get("run_at"),
            "next_run_at": state.get("next_run_at"),
            "repeat": str(state.get("repeat") or "once"),
            "count": int(state.get("count") or 1),
            "workers": max(1, min(2, int(state.get("workers") or 1))),
            "email_source": str(state.get("email_source") or "icloud"),
            "last_started_at": state.get("last_started_at"),
            "last_completed_at": state.get("last_completed_at"),
            "last_error": state.get("last_error"),
            "last_job_ids": state.get("last_job_ids") or [],
        }


def set_schedule(
    *,
    run_at: str,
    count: int = 1,
    workers: int = 1,
    repeat: str = "once",
    email_source: str = "icloud",
) -> dict[str, Any]:
    next_run = _parse_run_at(run_at)
    repeat_value = str(repeat or "once").strip().lower()
    if repeat_value not in {"once", "daily"}:
        raise ValueError("repeat 仅支持 once 或 daily")
    state = _load()
    state.update({
        "enabled": True,
        "status": "scheduled",
        "run_at": next_run.isoformat(),
        "next_run_at": next_run.isoformat(),
        "repeat": repeat_value,
        "count": max(1, min(200, int(count))),
        "workers": max(1, min(2, int(workers))),
        "email_source": str(email_source or "icloud").strip() or "icloud",
        "last_error": None,
    })
    with _LOCK:
        _save(state)
    start()
    return get_schedule()


def cancel_schedule() -> dict[str, Any]:
    with _LOCK:
        state = _load()
        state.update({"enabled": False, "status": "cancelled", "next_run_at": None})
        _save(state)
    return get_schedule()


def _run_due_once() -> None:
    with _LOCK:
        state = _load()
        if not bool(state.get("enabled")):
            return
        raw_next = state.get("next_run_at")
        try:
            next_run = _parse_run_at(raw_next)
        except (TypeError, ValueError):
            state.update({"enabled": False, "status": "failed", "last_error": "next_run_at 无效"})
            _save(state)
            return
        if _now() < next_run:
            return
        count = max(1, min(200, int(state.get("count") or 1)))
        workers = max(1, min(2, int(state.get("workers") or 1)))
        repeat = str(state.get("repeat") or "once")
        started_at = _now().isoformat()
        if repeat == "daily":
            state["next_run_at"] = (next_run + timedelta(days=1)).isoformat()
        else:
            state["enabled"] = False
            state["next_run_at"] = None
        state["status"] = "running"
        state["last_started_at"] = started_at
        state["last_error"] = None
        _save(state)

    try:
        from core import registration_service

        jobs = registration_service.submit_registration(
            count=count,
            email_source=str(state.get("email_source") or "icloud"),
            workers=workers,
        )
        job_ids = [int(item.get("id")) for item in jobs if item.get("id") is not None]
        with _LOCK:
            state = _load()
            state["status"] = "scheduled" if repeat == "daily" else "completed"
            state["last_completed_at"] = _now().isoformat()
            state["last_job_ids"] = job_ids
            _save(state)
        logger.info("[注册定时计划] 已提交 count=%s workers=%s jobs=%s", count, workers, job_ids)
    except Exception as exc:
        with _LOCK:
            state = _load()
            state["status"] = "scheduled" if repeat == "daily" else "failed"
            state["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            _save(state)
        logger.exception("[注册定时计划] 执行失败")


def _loop() -> None:
    while not _STOP.wait(1.0):
        try:
            _run_due_once()
        except Exception:
            logger.exception("[注册定时计划] 循环异常")


def start() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="registration-scheduler", daemon=True)
        _THREAD.start()


def stop() -> None:
    global _THREAD
    _STOP.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    _THREAD = None
