# -*- coding: utf-8 -*-
"""批量：账号+密码(+2FA) → 纯协议换取 AT/RT 并写回（不启动浏览器）。

用法：
    python tools/harvest_rt_by_password.py --min-id 285           # 指定起止
    python tools/harvest_rt_by_password.py --min-id 285 --limit 20
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-id", type=int, default=0)
    ap.add_argument("--max-id", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=4.0)
    ap.add_argument("--only-missing-rt", action="store_true", default=True)
    args = ap.parse_args()

    from core import db
    from core.password_login import login_with_password

    rows = []
    for row in db._load_accounts():
        acc_id = int(row.get("id") or 0)
        if args.min_id and acc_id < args.min_id:
            continue
        if args.max_id and acc_id > args.max_id:
            continue
        if not str(row.get("password") or "").strip():
            continue
        if args.only_missing_rt and str(row.get("chatgpt_refresh_token") or "").strip():
            continue
        rows.append(row)
    rows.sort(key=lambda r: int(r.get("id") or 0))
    if args.limit:
        rows = rows[: args.limit]

    print(f"目标 {len(rows)} 个账号（账号+密码，缺 RT）", flush=True)
    ok = fail = 0
    for idx, row in enumerate(rows, 1):
        email = str(row.get("email") or "")
        password = str(row.get("password") or "")
        totp = str(row.get("totp_secret") or "")
        country = ""
        extra = row.get("extra_json")
        if isinstance(extra, str):
            try:
                import json as _json
                extra = _json.loads(extra)
            except Exception:
                extra = {}
        if isinstance(extra, dict):
            country = str(extra.get("proxy_exit_country") or "")
        print(f"[{idx}/{len(rows)}] {email} (totp={'Y' if totp else 'N'}, country={country or '-'})", flush=True)
        try:
            res = login_with_password(
                email, password, totp_secret=totp, country_hint=country,
                write_back=True, timeout=30,
            )
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if res.get("ok"):
            ok += 1
            print(f"    OK rt={'Y' if res.get('refresh_token') else 'N'} ms={res.get('elapsed_ms')} steps={len(res.get('steps') or [])}", flush=True)
        else:
            fail += 1
            print(f"    FAIL {res.get('error')} | {str(res.get('detail'))[:120]}", flush=True)
        time.sleep(args.sleep + random.uniform(0, 2.0))
    print(f"完成：ok={ok} fail={fail} total={len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
