# -*- coding: utf-8 -*-
"""探针：纯协议「邮箱+密码(+2FA) → AT/RT」登录（不启动浏览器）。

用法：
    .\.venv\Scripts\python.exe tools\probe_password_login.py --account-id 280
    .\.venv\Scripts\python.exe tools\probe_password_login.py --account-id 280 --write-back
    .\.venv\Scripts\python.exe tools\probe_password_login.py --email a@b.c --password pw --totp SECRET --country SG
"""
import argparse
import json
import logging
import os
import sys

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _mask(text: str) -> str:
    text = str(text or "")
    return (text[:10] + "..." + text[-6:]) if len(text) > 20 else "***"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, default=0)
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--totp", default="", help="TOTP secret (base32)")
    ap.add_argument("--country", default="", help="出口国家提示（如 SG/US）")
    ap.add_argument("--write-back", action="store_true")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    email, password, totp, country = args.email, args.password, args.totp, args.country
    if args.account_id:
        from core import db
        rows = [r for r in db._load_accounts() if int(r.get("id") or 0) == args.account_id]
        if not rows:
            print(f"账号不存在: {args.account_id}")
            return 2
        acc = rows[0]
        email = email or str(acc.get("email") or "")
        password = password or str(acc.get("password") or "")
        totp = totp or str(acc.get("totp_secret") or "")
        country = country or str(acc.get("proxy_exit_country") or "")
        print(f"账号 {args.account_id}: email={email} has_pw={bool(password)} "
              f"has_totp={bool(totp)} country={country}")

    if not email or not password:
        print("缺少 email/password")
        return 2

    from core.password_login import login_with_password

    res = login_with_password(
        email, password, totp_secret=totp, country_hint=country,
        write_back=args.write_back, timeout=args.timeout,
    )
    if res.get("ok"):
        print("RESULT: OK")
        print("  email:", res.get("email"))
        print("  account_id:", res.get("account_id"))
        print("  at:", _mask(res.get("access_token")), "rt:", _mask(res.get("refresh_token")))
        print("  expires_at:", res.get("expires_at"), "elapsed_ms:", res.get("elapsed_ms"))
        print("  steps:", json.dumps(res.get("steps"), ensure_ascii=False))
        if "write_back" in res:
            print("  write_back:", json.dumps(res.get("write_back"), ensure_ascii=False))
        return 0
    print("RESULT: FAIL")
    print("  error:", res.get("error"))
    print("  detail:", json.dumps(res.get("detail"), ensure_ascii=False)[:900])
    print("  steps:", json.dumps(res.get("steps"), ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
