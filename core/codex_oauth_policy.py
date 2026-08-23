# -*- coding: utf-8 -*-
"""Codex OAuth 批次门禁，按账号年龄控制完整 OAuth 流程。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from config import codex as _cfg


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def evaluate_oauth_eligibility(
    account: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    min_age_days: int | None = None,
    require_expired_token: bool | None = None,
) -> dict[str, Any]:
    """判断账号是否允许执行完整 OAuth。

    规则由服务端执行，前端仅负责展示：
    1. 已有 ChatGPT OAuth refresh token 的账号不重复授权；
    2. 账号至少达到配置的最小年龄；
    3. 默认不检查 access token 到期时间；只有显式开启兼容开关时才额外限制。
    """
    row = account if isinstance(account, dict) else {}
    current = _parse_datetime(now) or datetime.now(timezone.utc)
    if min_age_days is None:
        min_age_days = getattr(_cfg, "CODEX_OAUTH_MIN_AGE_DAYS", 7)
    try:
        min_age_days = max(0, int(min_age_days))
    except (TypeError, ValueError):
        min_age_days = 7
    if require_expired_token is None:
        require_expired_token = bool(getattr(_cfg, "CODEX_OAUTH_REQUIRE_EXPIRED_TOKEN", False))

    if str(row.get("chatgpt_refresh_token") or "").strip():
        return {
            "eligible": False,
            "action": "plan_check",
            "reason_code": "oauth_already_persisted",
            "reason": "已有 ChatGPT refresh_token，无需重复 OAuth",
        }

    created_at = _parse_datetime(row.get("created_at"))
    eligible_at = created_at + timedelta(days=min_age_days) if created_at else None
    if eligible_at and current < eligible_at:
        return {
            "eligible": False,
            "action": "plan_check",
            "reason_code": "account_too_new",
            "reason": f"账号未达到最小等待期 {min_age_days} 天",
            "eligible_at": eligible_at.isoformat(),
        }
    if not created_at and min_age_days > 0:
        return {
            "eligible": False,
            "action": "plan_check",
            "reason_code": "created_at_missing",
            "reason": "缺少注册时间，不能执行完整 OAuth",
        }

    expires_at = _parse_datetime(
        row.get("token_expires_at")
        or row.get("chatgpt_token_expires_at")
        or row.get("access_token_expires_at")
    )
    token_expired = _as_bool(row.get("token_expired"))
    if require_expired_token:
        if expires_at and current < expires_at:
            return {
                "eligible": False,
                "action": "plan_check",
                "reason_code": "token_not_expired",
                "reason": "access_token 尚未到期，当前只允许轻量查活",
                "token_expires_at": expires_at.isoformat(),
            }
        if not expires_at and token_expired is False:
            return {
                "eligible": False,
                "action": "plan_check",
                "reason_code": "token_not_expired",
                "reason": "账号标记为 access_token 未到期，当前只允许轻量查活",
            }

    age_days = (current - created_at).total_seconds() / 86400 if created_at else None
    return {
        "eligible": True,
        "action": "oauth",
        "reason_code": "eligible",
        "reason": "达到 OAuth 批次条件",
        "age_days": round(age_days, 2) if age_days is not None else None,
        "eligible_at": eligible_at.isoformat() if eligible_at else None,
        "token_expires_at": expires_at.isoformat() if expires_at else None,
    }
