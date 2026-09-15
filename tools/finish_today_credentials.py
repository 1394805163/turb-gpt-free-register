# -*- coding: utf-8 -*-
"""今天新注册账号批量补齐凭据：密码(浏览器) -> 2FA(浏览器) -> RT(协议)。

分工（这是后续这类任务的标准方式）：
  补密码  = CloakBrowser 无头浏览器（reset-password 页面流程，需邮箱 OTP）
  补 2FA  = CloakBrowser 无头浏览器登录 + backend-api enroll/activate（需邮箱 OTP）
  取 RT   = 纯协议 login_with_password（authorize/PKCE + 密码 + TOTP，无浏览器、无邮箱码）

出口策略：按账号注册国家选路，节点轮换（避免连续两次落在同一个节点/IP）。
每一步可跳过已完成项；失败不中断，最后给汇总。

用法:
  .\\.venv\\Scripts\\python.exe tools\\finish_today_credentials.py --since 2026-09-15
  .\\.venv\\Scripts\\python.exe tools\\finish_today_credentials.py --ids 256 257 --gap 20
  .\\.venv\\Scripts\\python.exe tools\\finish_today_credentials.py --ids 256 --steps pwd
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Windows GBK 控制台遇到节点名 emoji 会 UnicodeEncodeError，这里兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LOG_DIR = os.path.join(WT, "run", "logs")


def _log(line: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {line}", flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"finish-credentials-{datetime.now().strftime('%Y%m%d')}.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except Exception:
        pass


def _pick_route(country: str, last_node: str):
    """按国家选出口，尽量避开上一个账号用的节点。"""
    from config import proxy as proxy_cfg

    selection = None
    for _ in range(3):
        selection = proxy_cfg.pick_registration_proxy(
            allowed_countries_override={country} if country else None
        )
        if str(selection.get("node_name") or "") != last_node:
            break
    return selection


def _account_row(acc_id: int) -> dict:
    from core import db

    return db.get_account(int(acc_id)) or {}


def _is_dead(row: dict) -> bool:
    codex = str(row.get("codex_status") or "").strip().lower()
    live = str(row.get("live_check_status") or "").strip().lower()
    return codex == "deactivated" or live == "confirmed_dead"


def _country(row: dict) -> str:
    import re

    explicit = str(row.get("proxy_exit_country") or "").strip().upper()
    if len(explicit) == 2:
        return explicit
    m = re.search(r"\b([A-Z]{2})\d*\b", str(row.get("proxy_used") or ""))
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="只要该日期之后注册的账号（如 2026-09-15）")
    ap.add_argument("--ids", nargs="*", type=int, default=[], help="显式账号 ID 列表")
    ap.add_argument("--steps", default="pwd,2fa,rt", help="执行哪些步骤，逗号分隔")
    ap.add_argument("--gap", type=float, default=30.0, help="账号之间的间隔秒")
    ap.add_argument("--max", type=int, default=0, help="最多处理几个账号（0=全部）")
    args = ap.parse_args()

    steps = {s.strip().lower() for s in str(args.steps).split(",") if s.strip()}
    from core import db

    rows = db._load_accounts()
    if args.ids:
        wanted = [int(x) for x in args.ids]
        targets = [r for r in rows if int(r.get("id") or 0) in wanted]
    else:
        if not args.since:
            ap.error("需要 --since 或 --ids")
        targets = [r for r in rows if str(r.get("created_at") or "") >= args.since]
    targets = sorted(targets, key=lambda r: int(r.get("id") or 0))
    if args.max:
        targets = targets[: int(args.max)]

    _log(f"批量补齐开始：{len(targets)} 个账号，步骤={sorted(steps)}")
    from core.account_password import set_account_password
    from core.account_2fa import set_account_2fa
    from core.password_login import login_with_password

    last_node = ""
    summary = {"pwd_ok": 0, "pwd_fail": 0, "2fa_ok": 0, "2fa_fail": 0, "rt_ok": 0, "rt_fail": 0, "skipped": []}

    for idx, row in enumerate(targets, 1):
        acc_id = int(row.get("id") or 0)
        email = str(row.get("email") or "").strip()
        tag = f"[{idx}/{len(targets)}] #{acc_id} {email}"
        if not email:
            summary["skipped"].append(f"#{acc_id} 无邮箱")
            continue
        if _is_dead(row):
            _log(f"{tag} 跳过（已判死）")
            summary["skipped"].append(f"#{acc_id} dead")
            continue

        country = _country(row) or "SG"
        route = _pick_route(country, last_node)
        node = str(route.get("node_name") or route.get("proxy_url") or "")
        last_node = node
        _log(f"{tag} 出口={node or '默认'} country_hint={country}")

        password = str(row.get("password") or row.get("registration_password") or "").strip()
        totp = str(row.get("totp_secret") or "").strip()
        rt = str(row.get("chatgpt_refresh_token") or row.get("refresh_token") or "").strip()

        # ---- 1) 补密码（无头浏览器）----
        if "pwd" in steps and not password:
            try:
                res = set_account_password(email, proxy_selection=route, save=True)
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            _log(f"{tag} 补密码 -> {'OK' if res.get('ok') else 'FAIL ' + str(res.get('error'))[:160]}")
            summary["pwd_ok" if res.get("ok") else "pwd_fail"] += 1
            if res.get("ok"):
                password = str(res.get("password") or "").strip()
            if not password:
                continue

        # ---- 2) 补 2FA（无头浏览器）----
        if "2fa" in steps and not totp:
            route2 = _pick_route(country, last_node)
            last_node = str(route2.get("node_name") or "")
            try:
                res = set_account_2fa(email, proxy_selection=route2, save=True)
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            _log(f"{tag} 补2FA -> {'OK' if res.get('ok') else 'FAIL ' + str(res.get('error'))[:160]}")
            summary["2fa_ok" if res.get("ok") else "2fa_fail"] += 1
            totp = str(res.get("totp_secret") or "") if res.get("ok") else ""

        # ---- 3) 取 RT（纯协议）----
        if "rt" in steps and not str(rt).startswith("rt"):
            if not password or not totp:
                _log(f"{tag} 取RT 跳过（password/2FA 不齐）")
                continue
            res = {}
            for attempt in range(1, 4):
                try:
                    res = login_with_password(
                        email, password,
                        totp_secret=totp,
                        country_hint=country,
                        write_back=True,
                    )
                except Exception as exc:
                    res = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                if res.get("ok"):
                    break
                if attempt < 3:
                    _log(f"{tag} 取RT 第{attempt}次失败（{str(res.get('error'))[:80]}），重试")
                    time.sleep(8 * attempt)
            ok = bool(res.get("ok"))
            detail = "OK" if ok else f"FAIL {str(res.get('error') or res.get('detail'))[:160]}"
            _log(f"{tag} 取RT -> {detail}")
            summary["rt_ok" if ok else "rt_fail"] += 1

        if idx < len(targets) and args.gap:
            time.sleep(max(0.0, float(args.gap)))

    _log(f"完成：{json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
