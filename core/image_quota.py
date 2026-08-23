# -*- coding: utf-8 -*-
"""ChatGPT image_gen 额度解析。网络请求由套餐查询会话复用。"""
from __future__ import annotations

from typing import Any


def extract_image_quota(payload: dict[str, Any] | None) -> dict[str, Any]:
    """从 conversation/init 响应提取 image_gen 剩余次数和恢复时间。"""
    data = payload if isinstance(payload, dict) else {}
    limits = data.get("limits_progress")
    if not isinstance(limits, list):
        limits = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        feature = str(
            item.get("feature_name") or item.get("feature") or item.get("name") or ""
        ).strip().lower()
        if feature not in {"image_gen", "image-generation", "image_generation"}:
            continue
        raw_remaining = item.get("remaining")
        try:
            remaining = max(0, int(float(raw_remaining)))
        except (TypeError, ValueError):
            remaining = None
        reset_at = str(
            item.get("reset_after") or item.get("reset_at") or item.get("reset_time") or ""
        ).strip() or None
        return {
            "image_quota": remaining,
            "image_quota_reset_at": reset_at,
            "image_quota_unknown": remaining is None,
        }
    return {
        "image_quota": None,
        "image_quota_reset_at": None,
        "image_quota_unknown": True,
    }
