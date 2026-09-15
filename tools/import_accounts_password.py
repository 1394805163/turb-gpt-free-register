# -*- coding: utf-8 -*-
r"""导入「邮箱+密码(+2FA)」→ 纯协议登录 → AT/RT 落库（不启动浏览器）。

输入文件每行：email----password----totp_secret
  分隔符支持 ---- / || / tab / 逗号；totp_secret 可省略（无 2FA 账号）。
行为：
  1. 逐个跑 core.password_login.login_with_password（串行，默认每个之间 sleep）
  2. 登录成功才写库：
     - 账号已在库 → 补密码/2FA + CAS 写回 AT/RT
     - 新账号    → insert_account 建记录 + 补密码 + 完整 OAuth 字段
  3. 失败只记录（不写库、不污染账号池）

用法：
  python tools\import_accounts_password.py --file accounts.txt --dry-run
  python tools\import_accounts_password.py --file accounts.txt --limit 5 --sleep 20
"""
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

SPLITTERS = ("----", "||", "\t", ",")


def parse_line(line: str) -> tuple[str, str, str] | None:
    """解析一行 -> (email, password, totp)；无效行返回 None。"""
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return None
    parts = None
    for sep in SPLITTERS:
        if sep in text:
            parts = [p.strip() for p in text.split(sep)]
            break
    if not parts:
        parts = text.split()
    if len(parts) < 2:
        return None
    email, password = parts[0].strip(), parts[1].strip()
    totp = parts[2].strip() if len(parts) > 2 else ""
    if "@" not in email or not password:
        return None
    return email, password, totp


def _load_items(path: str) -> list[tuple[str, str, str]]:
    items, seen = [], set()
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            parsed = parse_line(line)
            if parsed is None:
                continue
            key = parsed[0].lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(parsed)
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="输入文件（每行 email----password----totp）")
    ap.add_argument("--dry-run", action="store_true", help="只解析预览，不出网")
    ap.add_argument("--no-write", action="store_true", help="登录成功也不写库")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理 N 个（0=不限）")
    ap.add_argument("--sleep", type=float, default=20.0, help="每个账号之间的间隔秒数")
    ap.add_argument("--country", default="", help="强制出口国家（默认沿用账号库内 proxy_exit_country）")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        print("文件不存在:", args.file)
        return 2
    items = _load_items(args.file)
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("没有解析到有效账号行")
        return 2

    print(f"解析到 {len(items)} 个账号（dry-run={args.dry_run} no-write={args.no_write}）")
    if args.dry_run:
        for email, password, totp in items:
            print(f"  {email}  pw_len={len(password)}  totp={'有' if totp else '无'}")
        return 0

    from core import db
    from core.password_login import login_with_password

    started = datetime.now().isoformat(timespec="seconds")
    report = {"started_at": started, "total": len(items), "success": 0, "failed": 0, "items": []}

    for idx, (email, password, totp) in enumerate(items, 1):
        acc = db.get_account_by_email(email) or {}
        country = args.country or str(acc.get("proxy_exit_country") or "")
        print(f"[{idx}/{len(items)}] 登录 {email} (country={country or '-'}, totp={'有' if totp else '无'}) ...")
        res = login_with_password(
            email, password, totp_secret=totp, country_hint=country,
            write_back=False, timeout=args.timeout,
        )
        entry = {"email": email, "ok": bool(res.get("ok"))}
        if res.get("ok"):
            entry["elapsed_ms"] = res.get("elapsed_ms")
            entry["steps"] = [s.get("name") for s in res.get("steps") or []]
            if not args.no_write:
                write_result = {}
                try:
                    credential = {
                        "access_token": res["access_token"],
                        "refresh_token": res["refresh_token"],
                        "id_token": res["id_token"],
                        "oauth_client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                        "source": "password_login",
                        "expires_at": res.get("expires_at"),
                    }
                    if acc:
                        db.update_account_registration_password(email, password)
                        if totp:
                            db.update_account_totp_secret(email, totp)
                        old_at = str(acc.get("access_token") or acc.get("chatgpt_oauth_access_token") or "")
                        write_result = db.update_account_chatgpt_oauth(email, credential, expected_access_token=old_at)
                    else:
                        db.insert_account(
                            email=email,
                            access_token=res["access_token"],
                            totp_secret=totp or None,
                            chatgpt_oauth={
                                "access_token": res["access_token"],
                                "refresh_token": res["refresh_token"],
                                "id_token": res["id_token"],
                            },
                            email_source="import_password",
                        )
                        db.update_account_registration_password(email, password)
                        write_result = db.update_account_chatgpt_oauth(
                            email, credential, expected_access_token=res["access_token"])
                    entry["write"] = write_result
                except Exception as exc:
                    entry["write"] = {"updated": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
            report["success"] += 1
            print(f"    OK ({res.get('elapsed_ms')}ms) steps={entry['steps']} write={entry.get('write')}")
        else:
            entry["error"] = res.get("error")
            report["failed"] += 1
            print(f"    FAIL: {res.get('error')}")
        report["items"].append(entry)

        if idx < len(items) and args.sleep > 0:
            time.sleep(args.sleep)

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs("run", exist_ok=True)
    out_path = os.path.join("run", f"import-password-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\n完成：成功 {report['success']} / 失败 {report['failed']}，报告：{out_path}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) == 1:
        assert parse_line("a@b.com----pw123----JBSWY3DPEHPK3PXP") == ("a@b.com", "pw123", "JBSWY3DPEHPK3PXP")
        assert parse_line("a@b.com || pw456") == ("a@b.com", "pw456", "")
        assert parse_line("a@b.com,pw,my,totp")[:2] == ("a@b.com", "pw")
        assert parse_line("garbage line") is None
        assert parse_line("# comment") is None
        assert parse_line("") is None
        print("import_accounts_password self-check OK")
        sys.exit(0)
    sys.exit(main())


