# -*- coding: utf-8 -*-
"""从下游 chatgpt2api 只读同步账号状态（不触碰任何凭据）。

用途：
  - 已托管下游的账号（已推送/已导出）本地不再刷 RT；本工具读下游状态当"查活"。
  - 同步字段：downstream_*（状态、额度、AT/RT 状态、下游检查结果、最近使用时间）。
  - 全程只读 GET，不发送任何写入，不影响下游。

用法：
    python tools/sync_downstream_status.py --dry-run   # 只报告
    python tools/sync_downstream_status.py             # 写入本地库
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

import requests


def fetch_downstream(base: str, token: str, timeout: int = 20) -> dict:
    """拉取下游全量账号（按 email 索引）。"""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from config import chatgpt2api as cfg
    from core import db

    base = str(getattr(cfg, "CHATGPT2API_BASE_URL", "") or "").strip()
    token = str(getattr(cfg, "CHATGPT2API_ADMIN_KEY", "") or "").strip()
    if not base or not token:
        print("缺少 CHATGPT2API_BASE_URL / CHATGPT2API_ADMIN_KEY 配置")
        return 2

    down = fetch_downstream(base, token)
    print(f"下游账号: {len(down)}")

    from datetime import datetime
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
            if not args.dry_run:
                db._save_account_row(row)

    print(f"同步: 匹配 {updated} / 本地缺档 {missed} {'(dry-run, 未写库)' if args.dry_run else '(已写库)'}")
    print("下游状态分布:", dict(status_counter.most_common(10)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
