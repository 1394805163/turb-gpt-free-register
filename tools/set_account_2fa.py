# -*- coding: utf-8 -*-
"""给指定账号补设 2FA（TOTP；reauth 走邮箱 OTP，enroll/activate 走 backend-api）。

用法:
    python tools/set_account_2fa.py --ids 280            # 按账号 ID
    python tools/set_account_2fa.py a@b.com              # 按邮箱
"""
import argparse
import json
import os
import sys

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _emails_from_args(args) -> list[str]:
    from core import db

    emails = []
    for raw in args.ids or []:
        try:
            acc = db.get_account(int(raw))
        except Exception:
            acc = None
        if acc and acc.get("email"):
            emails.append(str(acc["email"]).strip())
    emails.extend(str(e).strip() for e in (args.emails or []) if str(e).strip())
    seen, out = set(), []
    for e in emails:
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("emails", nargs="*", help="按邮箱")
    ap.add_argument("--ids", nargs="*", help="按账号 ID")
    args = ap.parse_args()

    emails = _emails_from_args(args)
    if not emails:
        print("没有可处理的账号（用 --ids 或邮箱参数指定）")
        return 1
    print(f"待补设 2FA: {len(emails)} 个账号")

    from core.account_2fa import set_account_2fa

    ok_count = 0
    for email in emails:
        result = set_account_2fa(email)
        print(f"[{'OK' if result.get('ok') else 'FAIL'}] {email} {json.dumps(result, ensure_ascii=False)}")
        if result.get("ok"):
            ok_count += 1
    print(f"完成: 成功 {ok_count} / 失败 {len(emails) - ok_count}")
    return 0 if ok_count == len(emails) else 1


if __name__ == "__main__":
    sys.exit(main())
