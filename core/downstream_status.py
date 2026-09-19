# -*- coding: utf-8 -*-
"""从下游 chatgpt2api 只读同步账号状态（零凭据触碰）。

已托管下游的账号本地不再刷 RT；用本模块读下游状态当“查活”：
状态、额度、AT/RT 状态、最近检查结果、最近使用时间。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import requests


def fetch_downstream_accounts(base: str = "", token: str = "", timeout: int = 20) -> dict:
    """拉取下游全量账号（按 email 索引）。base/token 缺省时读配置。"""
    if not base or not token:
        from config import chatgpt2api as cfg
        base = base or str(getattr(cfg, "CHATGPT2API_BASE_URL", "") or "").strip()
        token = token or str(getattr(cfg, "CHATGPT2API_ADMIN_KEY", "") or "").strip()
    if not base or not token:
        raise RuntimeError("缺少 CHATGPT2API_BASE_URL / CHATGPT2API_ADMIN_KEY 配置")

    headers = {"Authorization": f"Bearer {token}"}
    out: dict = {}
    page, page_size = 1, 500
    while True:
        resp = requests.get(
            f"{base.rstrip('/')}/api/accounts",
            params={"page": page, "page_size": page_size},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items") or []
        for item in items:
            email = str(item.get("email") or "").strip().lower()
            if email:
                out[email] = item
        total = int(data.get("total") or 0)
        if not items or page * page_size >= total:
            break
        page += 1
    return out


def sync_downstream_status(*, dry_run: bool = False) -> dict:
    """把下游状态写入本地账号 downstream_* 字段；返回统计（dry_run 只统计不写库）。"""
    from core import db

    down = fetch_downstream_accounts()
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    missed = 0
    status_counter: Counter = Counter()
    with db._LOCK:
        for email, item in down.items():
            row = db._load_account_row(email=email)
            if row is None:
                missed += 1
                continue
            row["downstream_status"] = str(item.get("status_label") or "")
            row["downstream_status_category"] = str(item.get("status_category") or "")
            row["downstream_status_reason"] = str(item.get("status_reason") or "")
            row["downstream_quota_label"] = str(item.get("quota_label") or "")
            row["downstream_quota_remaining"] = item.get("quota_remaining")
            row["downstream_quota_reset_at"] = item.get("quota_reset_at")
            row["downstream_at_status"] = str(item.get("access_token_status") or "")
            row["downstream_rt_status"] = str(item.get("refresh_token_status") or "")
            row["downstream_last_check_result"] = str(item.get("last_remote_check_result") or "")
            row["downstream_last_checked_at"] = item.get("last_remote_checked_at")
            row["downstream_last_used_at"] = item.get("last_used_at")
            row["downstream_plan"] = str(item.get("plan_label") or "")
            row["downstream_synced_at"] = now
            status_counter[str(item.get("status_label") or "?")] += 1
            updated += 1
            if not dry_run:
                db._save_account_row(row)
    return {
        "downstream_total": len(down),
        "matched": updated,
        "missed": missed,
        "status_counts": dict(status_counter.most_common(10)),
        "synced_at": now,
        "dry_run": bool(dry_run),
    }
