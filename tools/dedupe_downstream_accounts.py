# -*- coding: utf-8 -*-
"""清理远端 chatgpt2api 上同邮箱的旧记录（我们推送产生的重复）。

远端没有删除账号的公开接口，可用路径：
  1) 旧记录先禁用（status=禁用）—— 停止被刷新/使用，任务记录不再刷 "refresh_token_reused"
  2) 再调用 import-cleanup(remove=true) 尝试删除（只删它判定为异常的）
"""
from __future__ import annotations

import collections
import os
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT); sys.path.insert(0, WT)

import httpx
from core import db

BASE = "http://129.225.181.1:3000"
H = {"Authorization": "Bearer 73612684", "Content-Type": "application/json"}


def main() -> int:
    ours = {str(r.get("email") or "").lower() for r in db._load_accounts()}
    items = httpx.get(BASE + "/api/accounts", headers=H, timeout=30).json().get("items") or []
    by_email = collections.defaultdict(list)
    for it in items:
        e = str(it.get("email") or "").lower()
        if e in ours:
            by_email[e].append(it)

    stale_ids = []
    for e, recs in by_email.items():
        if len(recs) < 2:
            continue
        recs.sort(key=lambda r: str(r.get("created_at") or ""))
        stale_ids.extend(str(r.get("id")) for r in recs[:-1])

    print(f"重复邮箱 {sum(1 for v in by_email.values() if len(v) > 1)} 个 | 待处理旧记录 {len(stale_ids)} 条")
    if not stale_ids:
        return 0

    r = httpx.post(BASE + "/api/accounts/batch-update", headers=H,
                   json={"account_ids": stale_ids, "status": "禁用"}, timeout=120)
    print("禁用旧记录:", r.status_code, (r.text or "")[:140])
    time.sleep(8)
    r2 = httpx.post(BASE + "/api/accounts/import-cleanup", headers=H,
                    json={"account_ids": stale_ids, "remove": True}, timeout=180)
    print("尝试清理:", r2.status_code, (r2.text or "")[:200])

    items2 = httpx.get(BASE + "/api/accounts", headers=H, timeout=30).json().get("items") or []
    c = collections.Counter(str(it.get("email") or "").lower() for it in items2 if str(it.get("email") or "").lower() in ours)
    dup_left = sum(1 for v in c.values() if v > 1)
    print(f"清理后：我们的记录 {sum(c.values())} 条 | 仍重复邮箱 {dup_left} 个（已禁用，不再刷新报错）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
