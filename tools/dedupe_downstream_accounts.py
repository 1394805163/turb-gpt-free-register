# -*- coding: utf-8 -*-
"""清理远端 chatgpt2api 同邮箱重复记录（分页 + 保留最优记录）。

保留规则：正常 > 禁用；AT valid > expiring > invalid；额度高者优先；最后取较新。
其余记录先禁用，再尝试 import-cleanup(remove=true) 删除。

用法：
    python tools/dedupe_downstream_accounts.py --dry-run
    python tools/dedupe_downstream_accounts.py
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT); sys.path.insert(0, WT)

import httpx
from config import chatgpt2api as cfg


def _fetch_all(base: str, headers: dict) -> list[dict]:
    items, page = [], 1
    while True:
        r = httpx.get(base + "/api/accounts", params={"page": page, "page_size": 500},
                      headers=headers, timeout=30)
        r.raise_for_status()
        d = r.json()
        items += d.get("items") or []
        if not d.get("items") or page * 500 >= int(d.get("total") or 0):
            return items
        page += 1


def _rank(rec: dict) -> tuple:
    status = 1 if str(rec.get("status_label")) == "正常" else 0
    at = {"valid": 2, "expiring": 1}.get(str(rec.get("access_token_status")), 0)
    quota = int(rec.get("quota_remaining") or 0)
    created = int(rec.get("created_at") or 0)
    return (status, at, quota, created)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = str(cfg.CHATGPT2API_BASE_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {cfg.CHATGPT2API_ADMIN_KEY}", "Content-Type": "application/json"}
    items = _fetch_all(base, headers)
    by_email = collections.defaultdict(list)
    for it in items:
        e = str(it.get("email") or "").strip().lower()
        if e:
            by_email[e].append(it)

    stale = []
    for e, recs in by_email.items():
        if len(recs) < 2:
            continue
        recs.sort(key=_rank, reverse=True)
        stale.extend(str(r.get("id")) for r in recs[1:])

    print(f"远端 {len(items)} 条 | 重复邮箱 {sum(1 for v in by_email.values() if len(v) > 1)} 个 | 待处理 {len(stale)} 条")
    if args.dry_run or not stale:
        return 0

    for i in range(0, len(stale), 100):
        chunk = stale[i:i + 100]
        r = httpx.post(base + "/api/accounts/batch-update", headers=headers,
                       json={"account_ids": chunk, "status": "禁用"}, timeout=120)
        print(f"禁用 {len(chunk)} 条 -> {r.status_code} {(r.text or '')[:100]}")
    removed = 0
    for i in range(0, len(stale), 100):
        chunk = stale[i:i + 100]
        r2 = httpx.request("DELETE", base + "/api/accounts", headers=headers,
                           json={"account_ids": chunk}, timeout=180)
        print(f"删除 {len(chunk)} 条 -> {r2.status_code} {(r2.text or '')[:100]}")
        if r2.status_code < 300:
            removed += len(chunk)
    print("已删除:", removed)

    left = _fetch_all(base, headers)
    c = collections.Counter(str(it.get("email") or "").strip().lower() for it in left)
    print(f"清理后：{len(left)} 条 | 仍重复邮箱 {sum(1 for v in c.values() if v > 1)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())

