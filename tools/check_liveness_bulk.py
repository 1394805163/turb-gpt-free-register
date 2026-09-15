# -*- coding: utf-8 -*-
r"""账号存活率快查（静默模式：只查套餐接口，不做任何额外账号接触）。

用法：
    python tools/check_liveness_bulk.py                  # 今天创建的全部账号
    python tools/check_liveness_bulk.py --date 2026-09-15
    python tools/check_liveness_bulk.py --ids 283,284
输出：控制台逐行 + 汇总存活率 + run/liveness-<date>-<HHMM>.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--ids", default="")
    args = ap.parse_args()

    from core import db
    from core.chatgpt_plan import check_account_plan

    rows = db._load_accounts()
    if args.ids:
        want = {int(x) for x in args.ids.split(",") if x.strip().isdigit()}
        targets = sorted([r for r in rows if int(r.get("id") or 0) in want], key=lambda x: int(x.get("id") or 0))
    else:
        targets = sorted(
            [r for r in rows if str(r.get("created_at") or "").startswith(args.date)],
            key=lambda x: int(x.get("id") or 0),
        )
    if not targets:
        print("没有匹配的账号")
        return 1

    print(f"目标 {len(targets)} 个账号，逐个静默查活 ...")
    out = []
    for i, r in enumerate(targets, 1):
        aid = int(r.get("id") or 0)
        email = str(r.get("email") or "")
        at = str(r.get("access_token") or r.get("chatgpt_oauth_access_token") or "")
        p = check_account_plan(at, timeout=40)
        if not p.get("ok") and p.get("http_status") in (403, 408, 429, 503, None):
            time.sleep(3)
            p = check_account_plan(at, timeout=40)
        entry = {"id": aid, "email": email, "ok": bool(p.get("ok")), "http": p.get("http_status"),
                 "quota": p.get("image_quota"), "err": str(p.get("error") or "")[:90],
                 "proxy": str(r.get("proxy_used") or "")}
        out.append(entry)
        print(f"[{i:02d}/{len(targets)}] id={aid} {'OK ' if entry['ok'] else 'FAIL'} "
              f"quota={entry['quota']} http={entry['http']} {email[:30]}")
        time.sleep(1.6)

    live = sum(1 for e in out if e["ok"])
    print("\n===== 汇总 =====")
    print(f"总: {len(out)} | 存活: {live} | 未通过: {len(out) - live} | 存活率: {live / len(out) * 100:.1f}%")
    for e in out:
        if not e["ok"]:
            print(f"  未通过: id={e['id']} {e['email'][:30]} http={e['http']} err={e['err'][:70]}")

    os.makedirs("run", exist_ok=True)
    fn = os.path.join("run", f"liveness-{args.date}-{datetime.now().strftime('%H%M')}.json")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("SAVED", fn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
