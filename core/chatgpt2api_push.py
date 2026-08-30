# -*- coding: utf-8 -*-
"""chatgpt2api 账号推送客户端、幂等状态和后台队列。"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests

from config import chatgpt2api as cfg
from core import db
from core.pipeline_concurrency import PIPELINE_MAX_CONCURRENCY, pipeline_slot


logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(
    max_workers=PIPELINE_MAX_CONCURRENCY,
    thread_name_prefix="chatgpt2api-push",
)
_QUEUE_LIMIT = 500
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def token_fingerprint(token: str) -> str:
    """返回可用于日志和幂等判断的短哈希，不暴露 token。"""
    value = str(token or "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _endpoint() -> str:
    base = str(getattr(cfg, "CHATGPT2API_BASE_URL", "") or "").strip().rstrip("/") + "/"
    return urljoin(base, "api/accounts") if base != "/" else ""


def _safe_error(*, response=None, exc: BaseException | None = None) -> str:
    # 不写响应正文和异常正文；两者都可能回显 Authorization、token 或请求体。
    if response is not None:
        return f"HTTP {int(getattr(response, 'status_code', 0) or 0)}"
    if exc is not None:
        return type(exc).__name__
    return "unknown_error"


def _retryable_status(status_code: int) -> bool:
    return int(status_code) in {408, 425, 429} or 500 <= int(status_code) <= 599


def _account_payload(account: dict) -> dict:
    """转换为 chatgpt2api/CPA 可识别的 Codex 凭据对象。

    CPA 导出使用 ``type=codex``、``email`` 和 OAuth 凭据字段。当前阶段保留
    旧 access token，新的凭据按新 token 写入；email 和完整凭据元数据同时
    传递，方便下游后续按邮箱归并。这里不发送注册机内部状态或邮箱池密钥。
    """
    access_token = str(
        account.get("access_token")
        or account.get("chatgpt_oauth_access_token")
        or ""
    ).strip()
    email_source = str(account.get("email_source") or "").strip().lower()
    oauth_refresh_token = str(account.get("chatgpt_refresh_token") or "").strip()
    # Outlook 的顶层 refresh_token 属于邮箱池登录凭据，不能当成 ChatGPT
    # OAuth refresh_token 推送；iCloud/其他邮箱的顶层字段才兼容旧格式。
    if not oauth_refresh_token and email_source != "outlook":
        oauth_refresh_token = str(account.get("refresh_token") or "").strip()
    id_token = str(account.get("chatgpt_id_token") or account.get("id_token") or "").strip()
    credential_kind = "complete" if oauth_refresh_token and id_token else "access_only"
    payload = {
        "type": "codex",
        "credential_kind": credential_kind,
        "email": str(account.get("email") or "").strip(),
        "access_token": access_token,
        "refresh_token": oauth_refresh_token,
        "id_token": id_token,
        "account_id": str(
            account.get("chatgpt_account_id")
            or account.get("account_id")
            or account.get("user_id")
            or ""
        ).strip(),
        "session_token": str(account.get("session_token") or "").strip(),
        "expired": str(
            account.get("chatgpt_token_expires_at")
            or account.get("token_expires_at")
            or account.get("expires_at")
            or ""
        ).strip(),
        "last_refresh": str(account.get("last_refresh") or "").strip(),
        "oauth_client_id": str(
            account.get("chatgpt_oauth_client_id")
            or account.get("oauth_client_id")
            or ""
        ).strip(),
        "oauth_status": str(account.get("oauth_status") or ("success" if credential_kind == "complete" else "access_only")).strip(),
        "email_source": str(account.get("email_source") or "").strip(),
        "email_pool_status": str(
            account.get("email_pool_status")
            or account.get("email_status")
            or ""
        ).strip(),
        "disabled": bool(account.get("disabled"))
        or str(account.get("status") or "").strip().lower() in {"disabled", "禁用"},
    }
    for key in ("user_id", "user_name", "plan_type", "current_plan_type", "status", "quota", "proxy"):
        value = account.get(key)
        if value not in (None, ""):
            payload[key] = value
    return {key: value for key, value in payload.items() if value not in (None, "")}


def push_account(
    account_id: int,
    *,
    expected_token_fingerprint: str | None = None,
    sleep_fn=time.sleep,
) -> dict:
    """同步推送一个已测活账号；用于后台 worker，也便于单元测试。"""
    account_id = int(account_id)
    if not bool(getattr(cfg, "CHATGPT2API_PUSH_ENABLED", False)):
        return {"ok": False, "status": "disabled", "disabled": True}

    account = db.get_account(account_id)
    if not account:
        return {"ok": False, "status": "missing", "error": "账号不存在"}
    token = str(account.get("access_token") or "").strip()
    fingerprint = token_fingerprint(token)
    if (
        expected_token_fingerprint is not None
        and str(expected_token_fingerprint) != fingerprint
    ):
        return {
            "ok": False,
            "status": "stale_token",
            "account_id": account_id,
            "error": "Token 已刷新，丢弃旧推送任务",
        }
    claim = db.claim_account_push(account_id, fingerprint)
    if claim == "idempotent":
        logger.info("[chatgpt2api] 幂等跳过 account_id=%s token_fp=%s token_len=%s", account_id, fingerprint, len(token))
        return {"ok": True, "status": "pushed", "idempotent": True, "account_id": account_id}
    if claim == "busy":
        return {"ok": False, "status": "busy", "busy": True, "account_id": account_id}
    if claim == "not_live":
        return {"ok": False, "status": "pending", "error": "账号尚未测活成功"}
    if claim == "stale_token":
        return {"ok": False, "status": "stale_token", "error": "Token 已刷新，丢弃旧推送任务"}
    if claim != "claimed":
        return {"ok": False, "status": "push_failed", "error": claim}

    endpoint = _endpoint()
    admin_key = str(getattr(cfg, "CHATGPT2API_ADMIN_KEY", "") or "").strip()
    if not endpoint or not admin_key:
        error = "chatgpt2api_config_incomplete"
        completed = db.complete_account_push(
            account_id,
            success=False,
            token_fingerprint=fingerprint,
            attempts=0,
            error=error,
        )
        if not completed:
            return {"ok": False, "status": "stale_token", "error": "Token 已刷新，丢弃旧推送任务"}
        logger.error(
            "[chatgpt2api] 配置不完整，已持久化失败 account_id=%s token_fp=%s token_len=%s",
            account_id,
            fingerprint,
            len(token),
        )
        return {"ok": False, "status": "push_failed", "error": error}

    max_attempts = max(1, int(getattr(cfg, "CHATGPT2API_MAX_RETRIES", 3) or 3))
    timeout = max(0.1, float(getattr(cfg, "CHATGPT2API_TIMEOUT", 10.0) or 10.0))
    backoff_base = max(0.0, float(getattr(cfg, "CHATGPT2API_BACKOFF_BASE", 1.0) or 0.0))
    headers = {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        # 远端 AccountCreateRequest 要求 tokens；accounts 用于保留 CPA OAuth
        # 三件套和 email 等账号元数据。当前服务端要求 tokens；保留旧 AT
        # 时允许新 token 形成新记录，避免在覆盖前丢失旧凭据。
        "tokens": [token],
        "accounts": [_account_payload(account)],
        "refresh_after_import": False,
    }
    last_error = "push_failed"
    last_http_status = None

    for attempt in range(1, max_attempts + 1):
        if not db.is_account_push_claim_current(account_id, fingerprint):
            return {
                "ok": False,
                "status": "stale_token",
                "account_id": account_id,
                "error": "Token 已刷新，丢弃旧推送任务",
            }
        response = None
        retryable = False
        try:
            response = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
            last_http_status = int(response.status_code)
            if 200 <= response.status_code < 300 or response.status_code == 409:
                completed = db.complete_account_push(
                    account_id,
                    success=True,
                    token_fingerprint=fingerprint,
                    attempts=attempt,
                    http_status=response.status_code,
                )
                if not completed:
                    logger.info(
                        "[chatgpt2api] 推送响应已过期，未覆盖新 Token 状态 account_id=%s token_fp=%s",
                        account_id,
                        fingerprint,
                    )
                    return {
                        "ok": False,
                        "status": "stale_token",
                        "account_id": account_id,
                        "error": "Token 已刷新，丢弃旧推送响应",
                    }
                logger.info(
                    "[chatgpt2api] 推送成功 account_id=%s token_fp=%s token_len=%s attempts=%s http=%s",
                    account_id, fingerprint, len(token), attempt, response.status_code,
                )
                return {
                    "ok": True,
                    "status": "pushed",
                    "account_id": account_id,
                    "attempts": attempt,
                    "http_status": response.status_code,
                }
            retryable = _retryable_status(response.status_code)
            last_error = _safe_error(response=response)
        except requests.RequestException as exc:
            retryable = True
            last_error = _safe_error(exc=exc)
        except Exception as exc:
            retryable = False
            last_error = _safe_error(exc=exc)

        if retryable and attempt < max_attempts:
            delay = backoff_base * (2 ** (attempt - 1))
            next_retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            recorded = db.record_account_push_attempt(
                account_id,
                attempt=attempt,
                error=last_error,
                next_retry_at=next_retry_at,
                http_status=last_http_status,
                token_fingerprint=fingerprint,
            )
            if not recorded:
                return {
                    "ok": False,
                    "status": "stale_token",
                    "account_id": account_id,
                    "error": "Token 已刷新，停止旧推送重试",
                }
            logger.warning(
                "[chatgpt2api] 临时失败，指数退避 account_id=%s token_fp=%s token_len=%s attempt=%s/%s wait=%.3fs error=%s",
                account_id, fingerprint, len(token), attempt, max_attempts, delay, last_error,
            )
            sleep_fn(delay)
            continue
        break

    completed = db.complete_account_push(
        account_id,
        success=False,
        token_fingerprint=fingerprint,
        attempts=attempt,
        error=last_error,
        http_status=last_http_status,
    )
    if not completed:
        return {
            "ok": False,
            "status": "stale_token",
            "account_id": account_id,
            "error": "Token 已刷新，丢弃旧推送结果",
        }
    logger.error(
        "[chatgpt2api] 推送失败 account_id=%s token_fp=%s token_len=%s attempts=%s error=%s",
        account_id, fingerprint, len(token), attempt, last_error,
    )
    return {
        "ok": False,
        "status": "push_failed",
        "account_id": account_id,
        "attempts": attempt,
        "http_status": last_http_status,
        "error": last_error,
    }


def _run_queued_push(
    account_id: int,
    expected_token_fingerprint: str | None = None,
) -> dict:
    try:
        with pipeline_slot("push"):
            return push_account(
                account_id,
                expected_token_fingerprint=expected_token_fingerprint,
            )
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_push(
    account_id: int,
    *,
    expected_token_fingerprint: str | None = None,
) -> dict:
    """把测活成功账号加入推送队列；队列线程仍受全局 2 槽闸门限制。"""
    if not bool(getattr(cfg, "CHATGPT2API_PUSH_ENABLED", False)):
        return {"accepted": False, "disabled": True, "status": "disabled"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "queue_full": True, "error": "推送队列已满"}
    try:
        _EXECUTOR.submit(
            _run_queued_push,
            int(account_id),
            expected_token_fingerprint,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "error": type(exc).__name__}
    return {"accepted": True, "account_id": int(account_id), "status": "queued"}


def resume_pending_pushes(limit: int = 500) -> dict:
    """进程启动后恢复已测活、尚未按当前 token 推送成功的账号。"""
    accepted = 0
    skipped = 0
    for account_id in db.list_push_candidate_ids(limit=limit):
        account = db.get_account(account_id)
        if not account:
            skipped += 1
            continue
        result = enqueue_account_push(
            account_id,
            expected_token_fingerprint=token_fingerprint(account.get("access_token") or ""),
        )
        if result.get("accepted"):
            accepted += 1
        else:
            skipped += 1
    return {"accepted": accepted, "skipped": skipped}


def queue_settings() -> dict:
    return {
        "workers": PIPELINE_MAX_CONCURRENCY,
        "queue_limit": _QUEUE_LIMIT,
        "shared_pipeline_limit": PIPELINE_MAX_CONCURRENCY,
    }
