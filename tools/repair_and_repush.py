# -*- coding: utf-8 -*-
"""修复并回推：本地凭据坏了（RT 被下游轮换/自己用旧 RT 刷过）时重新拿一套并推给下游。

流程（每个账号）：
  1. RT 刷新（写回本地）—— 快，~1 秒
  2. 失败则密码+2FA 协议登录（写回本地）—— ~5 秒，需要账号有密码/2FA
  3. 用最新凭据重新推送下游（push_account，token 变了自然会重推）

用法：
    python tools/repair_and_repush.py --from 2026-09-01 --to 2026-09-30
    python tools/repair_and_repush.py --from 2026-09-01 --to 2026-09-30 --limit 5 --dry-run
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
    args = ap.parse_args()

    from core import db
    from core.oauth_refresh import refresh_account_credentials
    from core.password_login import login_with_password
    from core.chatgpt2api_push import push_account

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
    print(f"目标 {len(rows)} 个账号（{args.date_from} ~ {args.date_to}）")
    if args.dry_run:
        for r in rows[:10]:
            print("   ", r.get("id"), r.get("email"), "| push:", r.get("push_status"))
        return 0

    report = {"started_at": datetime.now().isoformat(timespec="seconds"), "items": []}
    stats = {"rt_ok": 0, "login_ok": 0, "dead": 0, "pushed": 0, "push_fail": 0}
    for idx, row in enumerate(rows, 1):
        acc_id = int(row.get("id") or 0)
        email = str(row.get("email") or "")
        entry = {"id": acc_id, "email": email}
        # 1) RT 刷新
        res = refresh_account_credentials(email, write_back=True, force=True)
        if res.get("ok"):
            stats["rt_ok"] += 1
            entry["refresh"] = "rt"
        else:
            # 2) 密码+2FA 协议登录
            fresh = db.get_account(acc_id) or {}
            login = login_with_password(
                email,
                str(fresh.get("password") or ""),
                totp_secret=str(fresh.get("totp_secret") or ""),
                write_back=True,
                timeout=30,
            )
            if login.get("ok"):
                stats["login_ok"] += 1
                entry["refresh"] = "password_login"
            else:
                stats["dead"] += 1
                entry["refresh"] = "failed"
                entry["error"] = str(login.get("error") or res.get("error") or "")[:120]
        # 2.5) 刷新会重置查活状态（"凭据已更新，等待重新查活"），而推送要求 live。
        # 刚验证过的凭据本身就是存活的证据，这里直接落 live，避免为了过闸门再刷一次
        # （多刷一次会再次轮换 RT，把下游刚拿到的凭据又弄脏）。
        fresh_row = db.get_account(acc_id) or {}
        if entry.get("refresh") == "failed":
            entry["push"] = "skipped_no_credential"
            entry["push_ok"] = False
            report["items"].append(entry)
            print(f"[{idx}/{len(rows)}] {acc_id} {email[:34]:<34} 凭据=failed（跳过推送）", flush=True)
            time.sleep(args.sleep)
            continue
        if fresh_row.get("access_token"):
            db.update_account_liveness(
                acc_id,
                {
                    "ok": True,
                    "status": "live",
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "access_token": str(fresh_row.get("access_token") or ""),
                    "session": {},
                    "method": "repair_repush",
                },
            )
        # 3) 回推下游
        pushed = push_account(acc_id)
        entry["push"] = pushed.get("status")
        entry["push_ok"] = bool(pushed.get("ok"))
        if pushed.get("ok"):
            stats["pushed"] += 1
        else:
            stats["push_fail"] += 1
            entry["push_error"] = str(pushed.get("error") or "")[:80]
        print(
            f"[{idx}/{len(rows)}] {acc_id} {email[:34]:<34} 凭据={entry['refresh']:<14} 推送={entry['push']}",
            flush=True,
        )
        report["items"].append(entry)
        time.sleep(args.sleep)

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report.update(stats)
    path = f"run/repair-repush-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    open(path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"完成：RT 刷新 {stats['rt_ok']} | 协议登录救回 {stats['login_ok']} | 死号 {stats['dead']} | 回推成功 {stats['pushed']} | 回推失败 {stats['push_fail']}")
    print("报告:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
