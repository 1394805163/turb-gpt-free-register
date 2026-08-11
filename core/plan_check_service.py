# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan
from core.pipeline_concurrency import PIPELINE_MAX_CONCURRENCY, pipeline_slot

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", PIPELINE_MAX_CONCURRENCY, 1, PIPELINE_MAX_CONCURRENCY)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="plan-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _run_plan_check_inner(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
) -> dict:
    expected_token_fingerprint = db.token_fingerprint(access_token)
    try:
        if not db.mark_account_plan_check_running(
            account_id,
            expected_token_fingerprint=expected_token_fingerprint,
        ):
            return {
                "ok": False,
                "stale_result_discarded": True,
                "error": "账号已删除、Token 已刷新或套餐查询状态已被重置",
            }

        _wait_for_rate_slot()
        result = check_account_plan(
            access_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
        )

        recheck_delay = _registration_recheck_delay()
        should_recheck = (
            trigger == "registration_auto"
            and recheck_delay > 0
            and bool(result.get("ok"))
            and str(result.get("current_plan_type") or "").lower() == "free"
            and not bool(result.get("plus_trial_eligible"))
        )
        if should_recheck:
            logger.info("[Plan] 新账号暂未发现 Plus 试用资格，%.1fs 后复查一次: %s", recheck_delay, email)
            time.sleep(recheck_delay)
            _wait_for_rate_slot()
            recheck_result = check_account_plan(
                access_token,
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
                max_attempts=1,
            )
            if recheck_result.get("ok"):
                result = recheck_result
            else:
                logger.warning(
                    "[Plan] 新账号资格复查失败，保留首次成功结果: %s, %s",
                    email,
                    recheck_result.get("error") or "未知错误",
                )

        plan_updated = db.update_account_plan_check(
            acc_id=account_id,
            result=result,
            expected_token_fingerprint=expected_token_fingerprint,
        )
        if not plan_updated:
            logger.info(
                "[Plan] 丢弃旧 Token 的套餐查询结果: account_id=%s token_fp=%s",
                account_id,
                expected_token_fingerprint,
            )
            return {**result, "stale_result_discarded": True}
        if result.get("ok"):
            live_updated = db.update_account_liveness(account_id, {
                "ok": True,
                "status": "live",
                "method": "token",
                "checked_at": result.get("checked_at"),
                "access_token": access_token,
            }, expected_token_fingerprint=expected_token_fingerprint)
            if live_updated:
                try:
                    from core.chatgpt2api_push import enqueue_account_push

                    push_queued = enqueue_account_push(
                        account_id,
                        expected_token_fingerprint=expected_token_fingerprint,
                    )
                    if push_queued.get("accepted"):
                        logger.info("[Plan] Token 快速测活成功，推送已入队: account_id=%s", account_id)
                    elif not push_queued.get("disabled"):
                        logger.warning(
                            "[Plan] Token 快速测活成功，但推送未入队: account_id=%s error=%s",
                            account_id,
                            push_queued.get("error") or push_queued.get("status") or "unknown",
                        )
                except Exception as push_exc:
                    # 推送是独立下游；入队异常不能覆盖已经确认成功的 Token/套餐结果。
                    logger.warning(
                        "[Plan] Token 快速测活成功，但推送入队异常: account_id=%s error=%s",
                        account_id,
                        type(push_exc).__name__,
                    )
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
        else:
            if result.get("needs_live_check") is True:
                db.update_account_liveness(account_id, {
                    "ok": False,
                    "status": "temporary_error",
                    "method": "token",
                    "checked_at": result.get("checked_at"),
                    "needs_live_check": True,
                    "error": result.get("error") or "当前 Token 需要完整登录刷新",
                }, expected_token_fingerprint=expected_token_fingerprint)
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(
                acc_id=account_id,
                result=result,
                expected_token_fingerprint=expected_token_fingerprint,
            )
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def _run_plan_check(**kwargs) -> dict:
    """注册后套餐查询与注册、测活、推送共用全局两个工作槽。"""
    with pipeline_slot("plan_check"):
        return _run_plan_check_inner(**kwargs)


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    expected_token_fingerprint = db.token_fingerprint(access_token)
    if not db.claim_account_plan_check(
        acc_id=account_id,
        trigger=trigger,
        expected_token_fingerprint=expected_token_fingerprint,
    ):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    try:
        _EXECUTOR.submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(
            acc_id=account_id,
            result=result,
            expected_token_fingerprint=expected_token_fingerprint,
        )
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
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "shared_pipeline_limit": PIPELINE_MAX_CONCURRENCY,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
