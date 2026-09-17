# -*- coding: utf-8 -*-
"""把指定日期（默认 9 月）注册的账号推送到 chatgpt2api。

用法：
    python tools/push_accounts_by_date.py --from 2026-09-01 --to 2026-09-30
    python tools/push_accounts_by_date.py --from 2026-09-15 --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2026-09-01")
    ap.add_argument("--to", dest="date_to", default="2026-09-30")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--allow-no-password",
        action="store_true",
        help="没有密码+2FA 的账号也推送（默认跳过：推送后下游会用复用的 RT 轮换，本地再也拿不回新凭据）",
    )
    args = ap.parse_args()

    from config import chatgpt2api as cfg
    print(f"远端: {getattr(cfg, 'CHATGPT2API_BASE_URL', '')} | push_enabled={getattr(cfg, 'CHATGPT2API_PUSH_ENABLED', False)}")
    if args.dry_run:
        print("dry-run：只列出目标账号，不推送")

    from core import db

    rows = []
    for row in db._load_accounts():
        created = str(row.get("created_at") or "")[:10]
        if not (args.date_from <= created <= args.date_to):
            continue
        if not str(row.get("access_token") or "").strip():
            continue
        rows.append(row)
    rows.sort(key=lambda r: int(r.get("id") or 0))
    if args.limit:
        rows = rows[: args.limit]
    if not args.allow_no_password:
        no_recovery = [
            r for r in rows
            if not str(r.get("password") or "").strip() or not str(r.get("totp_secret") or "").strip()
        ]
        if no_recovery:
            print(f"跳过 {len(no_recovery)} 个没有密码/2FA 的账号（推送后本地无法用协议登录补回凭据）")
            for r in no_recovery[:5]:
                print("    跳过:", r.get("id"), r.get("email"))
            rows = [r for r in rows if r not in no_recovery]
    print(f"目标账号 {len(rows)} 个（{args.date_from} ~ {args.date_to}）")

    if args.dry_run:
        for r in rows[:10]:
            print("   ", r.get("id"), r.get("email"), "| live:", r.get("live_check_status"), "| push:", r.get("push_status"))
        return 0

    from core.chatgpt2api_push import push_account

    ok = fail = skipped = 0
    report = {"started_at": datetime.now().isoformat(timespec="seconds"), "items": []}
    for idx, row in enumerate(rows, 1):
        acc_id = int(row.get("id") or 0)
        email = str(row.get("email") or "")
        res = push_account(acc_id)
        status = str(res.get("status") or "")
        flag = "OK" if res.get("ok") else ("SKIP" if status in ("pushed",) or res.get("idempotent") else "FAIL")
        if res.get("ok"):
            ok += 1
            if res.get("idempotent"):
                skipped += 1
        else:
            fail += 1
        print(f"[{idx}/{len(rows)}] {flag} {acc_id} {email} | {status} | {str(res.get('error') or '')[:60]}", flush=True)
        report["items"].append({"id": acc_id, "email": email, "ok": bool(res.get("ok")), "status": status, "error": res.get("error")})
        time.sleep(args.sleep)

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["ok"] = ok
    report["fail"] = fail
    report["idempotent"] = skipped
    path = f"run/push-september-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    open(path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"完成：成功 {ok}（其中幂等 {skipped}）| 失败 {fail} | 报告 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
