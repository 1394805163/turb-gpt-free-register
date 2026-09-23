# -*- coding: utf-8 -*-
"""新增：协议版"补密码"服务（后台队列 + 独立日志文件），供 WebUI 按钮与"日志合一"使用。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKERS = 2
_QUEUE_LIMIT = 200
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="addpw")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[str] = set()
_LOCK = threading.Lock()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"add-password-{safe}.log"


def is_running(email: str) -> bool:
    with _LOCK:
        return str(email or "").strip().lower() in _RUNNING


def _run(email: str, trigger: str) -> dict:
    key = str(email or "").strip().lower()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    thread_name = threading.current_thread().name
    fh.addFilter(lambda record: record.threadName == thread_name)
    root = logging.getLogger()
    root.addHandler(fh)
    try:
        from core.account_password import add_password_protocol

        logger.info("[补密码] 任务开始（trigger=%s）", trigger)
        result = add_password_protocol(email)
        logger.info("[补密码] 任务结束：%s | %s", result.get("status"),
                    str(result.get("error") or "-")[:200])
        if result.get("ok"):
            logger.info("[补密码] 密码已写回账号，可点「查活刷新AT」用账密+2FA 换新 AT")
        return {
            "status": str(result.get("status") or ""),
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or "")[:200],
        }
    except Exception as exc:  # noqa: BLE001 - 后台任务不允许把异常抛给线程池
        logger.exception("[补密码] 任务异常")
        return {"status": "failed", "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        try:
            root.removeHandler(fh)
            fh.close()
        except Exception:
            pass
        with _LOCK:
            _RUNNING.discard(key)
        try:
            _QUEUE_SLOTS.release()
        except Exception:
            pass


def enqueue_add_password(*, account_id: int = 0, email: str, trigger: str = "manual") -> dict:
    """把"协议补密码"任务放后台队列；同一邮箱不允许并发。"""
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    key = email.lower()
    with _LOCK:
        if key in _RUNNING:
            return {"accepted": False, "busy": True, "error": "该账号正在补密码"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "补密码队列已满，请稍后重试"}
    with _LOCK:
        _RUNNING.add(key)
    try:
        _EXECUTOR.submit(_run, email, str(trigger or "manual"))
    except Exception as exc:
        with _LOCK:
            _RUNNING.discard(key)
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"accepted": True, "busy": False, "account_id": int(account_id or 0), "email": email}


def queue_settings() -> dict:
    with _LOCK:
        running = len(_RUNNING)
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT, "running": running}


if __name__ == "__main__":
    # 最小自检：日志路径与队列占用语义（离线）
    p = log_path("demo@example.com")
    assert p.parent.name == "注册日志" and p.name.startswith("add-password-"), p
    assert log_path("a/b:c@x.com").name in {"add-password-a_b_c@x.com.log", "add-password-a_b_c_x.com.log"}
    assert is_running("nobody@example.com") is False
    assert enqueue_add_password(email="")["accepted"] is False
    print("add_password_service self-check OK:", p.name)
