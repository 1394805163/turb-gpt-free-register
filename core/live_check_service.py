# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import resolve_plan_check_route
from core.pipeline_concurrency import PIPELINE_MAX_CONCURRENCY, pipeline_slot

logger = logging.getLogger(__name__)

_WORKERS = PIPELINE_MAX_CONCURRENCY
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _resolve_live_check_route(proxy: str | None) -> dict:
    """查活跟随注册出口策略；Resin 关闭时只允许 Mihomo 美国组。"""
    from config import proxy as proxy_cfg

    if not bool(getattr(proxy_cfg, "REGISTRATION_PROXY_REQUIRED", False)):
        selection = proxy_cfg.pick_registration_proxy()
        selected = str(selection.get("proxy_url") or "")
        transparent = bool(selection.get("transparent"))
        return {
            "proxy": selected,
            "proxy_mode": "mihomo_us_transparent" if transparent else "mihomo_us",
            "network_route": "transparent" if transparent else "proxy",
            "proxy_used": selected or ("router-policy" if transparent else ""),
            "proxy_fallback_reason": None,
            "proxy_group": selection.get("group"),
            "proxy_node": selection.get("node_name"),
        }
    return resolve_plan_check_route(explicit_proxy=proxy)


def _run_live_check_inner(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        route = _resolve_live_check_route(proxy)
        selected_proxy = route.get("proxy")
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"group={route.get('proxy_group') or '-'} node={route.get('proxy_node') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        result = check_account_liveness(
            email,
            proxy=selected_proxy,
            clear_log=False,
            rotate_transparent_route=route.get("network_route") == "transparent",
        )
        db.update_account_liveness(account_id, result)
        if result.get("ok"):
            _append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
            try:
                from core.chatgpt2api_push import enqueue_account_push
                pushed = enqueue_account_push(account_id)
                if pushed.get("accepted"):
                    _append_log(email, "[推送] 测活成功，已进入 chatgpt2api 推送队列")
                elif not pushed.get("disabled"):
                    _append_log(email, f"[推送] 入队跳过：{pushed.get('error') or pushed.get('status') or 'unknown'}")
            except Exception as exc:
                _append_log(email, f"[推送] 入队异常：{type(exc).__name__}")
        elif result.get("status") == "confirmed_dead":
            _append_log(email, f"[查活] 完成：确认死亡 {result.get('error') or ''}")
        else:
            _append_log(email, f"[查活] 完成：临时错误 {result.get('error') or ''}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "temporary_error",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    with pipeline_slot("live_check"):
        return _run_live_check_inner(
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=trigger,
        )


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "temporary_error",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
