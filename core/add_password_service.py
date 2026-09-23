# -*- coding: utf-8 -*-
"""新增：协议版"补密码"服务（后台队列 + 独立日志文件），供 WebUI 按钮与"日志合一"使用。"""
from __future__ import annotations

import logging
import threading
import time
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


_SWEEP_INTERVAL_SECONDS = 300
_SWEEP_MIN_AGE_MINUTES = 15.0
_SWEEP_LIMIT = 2
_MAX_ATTEMPTS = 3
_sweeper_started = False
_sweeper_lock = threading.Lock()


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
        try:
            from core import db

            if result.get("ok"):
                db.set_account_password_pending(email, status="done", reason="补密码成功")
            else:
                acc = db.get_account_by_email(email) or {}
                attempts = int(acc.get("password_pending_attempts") or 0)
                if attempts >= _MAX_ATTEMPTS:
                    db.set_account_password_pending(
                        email, status="failed",
                        reason=f"补密码连续失败 {attempts} 次：{str(result.get('error') or '')[:100]}")
                else:
                    db.set_account_password_pending(
                        email, status="pending",
                        reason=f"待重试({attempts}/{_MAX_ATTEMPTS})：{str(result.get('error') or '')[:100]}")
        except Exception as exc:
            logger.warning("[补密码] 挂号状态回写失败：%s", str(exc)[:140])
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


# 这些阶段在跑时不要补密码：补密码要挑/切 mihomo 节点，会让正在跑的注册
# 中途换出口（新号几分钟内跨国跳变是最典型的风控特征）。
_NODE_SWITCHING_STAGES = ("registration", "live_check", "codex_oauth")


def _busy_stages() -> list[str]:
    try:
        from core.pipeline_concurrency import pipeline_snapshot

        stages = pipeline_snapshot().get("by_stage") or {}
        return sorted(name for name in _NODE_SWITCHING_STAGES if int(stages.get(name) or 0) > 0)
    except Exception:
        return []


def sweep_pending(*, limit: int = _SWEEP_LIMIT, min_age_minutes: float = _SWEEP_MIN_AGE_MINUTES) -> dict:
    """扫描"注册后待补密码"的账号并补跑。

    只挑：挂号 pending、没有密码、账号没死、有 access_token、并且注册已满
    min_age_minutes（默认 15 分钟，避开注册后立刻补密码撞 rate_limit_exceeded）。
    """
    busy = _busy_stages()
    if busy:
        logger.info("[补密码] 扫描器跳过本轮：注册/查活/Codex 授权进行中 %s（避免切节点打断出口）", busy)
        return {"scanned": 0, "picked": 0, "queued": [], "skipped_reason": "pipeline_busy", "busy": busy}

    from datetime import datetime, timedelta

    from core import db

    rows = db.list_accounts()
    now = datetime.now()
    picked: list[dict] = []
    for acc in rows:
        if str(acc.get("password_pending_status") or "").strip().lower() != "pending":
            continue
        email = str(acc.get("email") or "").strip()
        if not email:
            continue
        # 先判死：死号即使本地挂着（可能是失败时登记的）假密码也不该继续补。
        if str(acc.get("live_check_status") or "").strip().lower() in {"confirmed_dead", "deactivated"}:
            db.set_account_password_pending(email, status="skipped", reason="账号已废")
            continue
        # 注意：不能拿"本地有 password"当成功依据 —— 补密码失败时会先行登记候选值，
        # 真假难分。成功/失败只认 _run 跑完后的写回（done / pending / failed）。
        if not str(acc.get("access_token") or "").strip():
            continue
        try:
            created = datetime.fromisoformat(str(acc.get("created_at") or ""))
        except (TypeError, ValueError):
            continue
        if now - created < timedelta(minutes=float(min_age_minutes)):
            continue
        picked.append(acc)
        if len(picked) >= max(1, int(limit)):
            break

    queued: list[str] = []
    for acc in picked:
        email = str(acc.get("email") or "")
        result = enqueue_add_password(
            account_id=int(acc.get("id") or 0), email=email, trigger="registration_auto_sweep")
        if result.get("accepted"):
            db.set_account_password_pending(email, status="in_progress", reason="已入队补密码")
            queued.append(email)
        logger.info("[补密码] 扫描器：%s -> %s", email,
                    result.get("status") or result.get("error") or ("queued" if result.get("accepted") else "?"))
    return {"scanned": len(rows), "picked": len(picked), "queued": queued}


def start_sweeper(*, interval: int = _SWEEP_INTERVAL_SECONDS) -> None:
    """启动常驻扫描线程（WebUI 进程内跑；重复调用只会启动一次）。"""
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

    def _loop() -> None:
        time.sleep(60)  # 首轮延后，避开 WebUI 启动高峰
        while True:
            try:
                result = sweep_pending()
                if result.get("queued"):
                    logger.info("[补密码] 扫描器本轮入队 %s 个：%s", len(result["queued"]), result["queued"])
            except Exception:
                logger.exception("[补密码] 扫描器异常")
            time.sleep(max(60, int(interval)))

    threading.Thread(target=_loop, name="addpw-sweeper", daemon=True).start()
    logger.info("[补密码] 自动补密码扫描器已启动：间隔 %ss，最小账号年龄 %s 分钟",
                interval, _SWEEP_MIN_AGE_MINUTES)
