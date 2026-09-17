# -*- coding: utf-8 -*-
"""把一批账号的「密码 + 2FA」收口干完（浏览器席位串行，重复跑幂等）。

用法：
    python tools/finish_batch_credentials.py --min-id 285
    python tools/finish_batch_credentials.py --min-id 285 --only-password
    python tools/finish_batch_credentials.py --min-id 285 --only-2fa
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import random
import sys
import time
from datetime import datetime

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("finish-creds")

STATE_PATH = os.path.join(WT, "data", "overnight_pipeline_state.json")
REPORT_DIR = os.path.join(WT, "run")


def _sync_state(email: str, **fields) -> None:
    """把结果同步进流水线状态，避免总控再跑一遍。"""
    try:
        st = json.load(io.open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return
    changed = False
    for item in st.get("accounts") or []:
        if str(item.get("email") or "") != email:
            continue
        item.update(fields)
        changed = True
    if changed:
        try:
            io.open(STATE_PATH, "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:
            logger.warning("状态同步失败：%s", str(exc)[:120])


def _run_with_license_backoff(fn, *, label: str, max_waits: int = 30, wait_seconds: float = 75.0):
    for i in range(max_waits + 1):
        result = fn() or {}
        if result.get("status") != "license_busy":
            return result
        logger.warning("[%s] 浏览器席位居满，%ss 后重试（第 %s 次）", label, int(wait_seconds), i + 1)
        time.sleep(wait_seconds)
    return {"ok": False, "status": "license_busy", "error": "席位等待超限"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-id", type=int, default=285)
    ap.add_argument("--max-id", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=3.0)
    ap.add_argument("--only-password", action="store_true")
    ap.add_argument("--only-2fa", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from core import db
    from core.account_password import set_account_password
    from core.account_2fa import set_account_2fa

    targets = []
    for row in db._load_accounts():
        acc_id = int(row.get("id") or 0)
        if acc_id < args.min_id:
            continue
        if args.max_id and acc_id > args.max_id:
            continue
        need_pw = not str(row.get("password") or "").strip()
        need_2fa = not str(row.get("totp_secret") or "").strip()
        if args.only_password:
            need_2fa = False
        if args.only_2fa:
            need_pw = False
        if need_pw or need_2fa:
            targets.append({"id": acc_id, "email": str(row.get("email") or ""), "need_pw": need_pw, "need_2fa": need_2fa})
    targets.sort(key=lambda x: x["id"])
    if args.limit:
        targets = targets[: args.limit]

    print(f"待收口账号 {len(targets)} 个（补密码 {sum(1 for t in targets if t['need_pw'])}，补2FA {sum(1 for t in targets if t['need_2fa'])}）", flush=True)
    report = {"started_at": datetime.now().isoformat(timespec="seconds"), "targets": len(targets), "items": []}
    ok_pw = ok_2fa = 0
    for idx, item in enumerate(targets, 1):
        email = item["email"]
        entry = {"id": item["id"], "email": email}
        print(f"[{idx}/{len(targets)}] {email} pw={item['need_pw']} 2fa={item['need_2fa']}", flush=True)

        if item["need_pw"]:
            res = _run_with_license_backoff(lambda: set_account_password(email), label=f"补密码 {email}")
            entry["password"] = {k: v for k, v in res.items() if k != "password"}
            if res.get("ok"):
                ok_pw += 1
                _sync_state(email, password_ok=True, password_status="ok_tool", password=str(res.get("password") or ""))
                print("    密码 OK", flush=True)
            else:
                status = str(res.get("status") or "failed")
                _sync_state(email, password_status=f"failed_tool:{status}")
                print(f"    密码 FAIL {status} | {str(res.get('error'))[:80]}", flush=True)

        if item["need_2fa"]:
            res = _run_with_license_backoff(lambda: set_account_2fa(email), label=f"补2FA {email}")
            entry["twofa"] = {k: v for k, v in res.items() if k != "totp_secret"}
            if res.get("ok"):
                ok_2fa += 1
                _sync_state(email, twofa_ok=True, twofa_status="ok_tool")
                print("    2FA OK", flush=True)
            else:
                status = str(res.get("status") or "failed")
                _sync_state(email, twofa_status=f"failed_tool:{status}")
                print(f"    2FA FAIL {status} | {str(res.get('error'))[:80]}", flush=True)

        report["items"].append(entry)
        time.sleep(args.sleep + random.uniform(0, 2.0))

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["ok_password"] = ok_pw
    report["ok_2fa"] = ok_2fa
    path = os.path.join(REPORT_DIR, f"finish-credentials-{datetime.now().strftime('%Y%m%d-%H%M')}.json")
    try:
        io.open(path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        path = ""
    print(f"完成：密码 OK {ok_pw} | 2FA OK {ok_2fa} | 报告：{path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
