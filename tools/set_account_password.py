# -*- coding: utf-8 -*-
"""给指定账号补设密码（重置流程；逐个人工节奏执行）。

用法:
    python tools/set_account_password.py --ids 228 230            # 按账号 ID
    python tools/set_account_password.py a@b.com c@d.com          # 按邮箱
    python tools/set_account_password.py --ids 228 --dry-run      # 只跑流程不落盘
"""
import argparse
import json
import os
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _emails_from_args(args) -> list[str]:
    from core import db

    emails: list[str] = []
    for raw in args.ids or []:
        acc = db.get_account(int(raw))
        if not acc:
            print(f"  [跳过] 账号不存在: {raw}", flush=True)
            continue
        emails.append(str(acc.get("email") or ""))
    emails.extend(str(x).strip() for x in (args.emails or []) if str(x).strip())
    seen, ordered = set(), []
    for email in emails:
        if email and email not in seen:
            seen.add(email)
            ordered.append(email)
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("emails", nargs="*", help="账号邮箱")
    parser.add_argument("--ids", nargs="*", type=int, help="账号 ID")
    parser.add_argument("--password", default="", help="指定密码；留空则自动生成")
    parser.add_argument("--dry-run", action="store_true", help="不写库")
    parser.add_argument("--gap", type=float, default=5.0, help="账号之间的间隔秒数")
    args = parser.parse_args()

    from core.account_password import set_account_password

    emails = _emails_from_args(args)
    if not emails:
        parser.error("请提供 --ids 或邮箱")
    print(f"待补设密码: {len(emails)} 个账号", flush=True)

    ok = failed = 0
    for idx, email in enumerate(emails, 1):
        result = set_account_password(email, password=args.password or None, save=not args.dry_run)
        flag = "OK " if result.get("ok") else "FAIL"
        print(f"[{idx}/{len(emails)}] {flag} {email} {json.dumps({k: v for k, v in result.items() if k != 'email'}, ensure_ascii=False)[:220]}", flush=True)
        ok += 1 if result.get("ok") else 0
        failed += 0 if result.get("ok") else 1
        if idx < len(emails) and args.gap:
            time.sleep(max(0.0, args.gap))
    print(f"完成: 成功 {ok} / 失败 {failed}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())