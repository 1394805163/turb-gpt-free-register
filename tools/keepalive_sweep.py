# -*- coding: utf-8 -*-
"""保活扫描：用 RT 批量续 AT，可挂在探针/定时任务上跑。

只处理"本地托管"的临期账号：下游 RT 有效（downstream_rt_status=valid）
说明下游自己会续期，本地再刷就是抢同一条一次性 RT → 直接跳过。
AT-only 推送的账号下游没有 RT，本地是唯一刷新方，才纳入保活。
同一账号连续失败 3 次后跳过，避免坏 RT 每轮白打接口。

用法：
    python tools/keepalive_sweep.py --days 3 --limit 40
    python tools/keepalive_sweep.py --dry-run
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

STATE = "run/keepalive_state.json"
MAX_FAILS = 3
DEAD = {"confirmed_dead", "deactivated"}


def at_exp(token: str) -> int:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part)).get("exp") or 0)
    except Exception:
        return 0


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs("run", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def candidates(days: float, limit: int, skip_ids: set[int]) -> list[dict]:
    from core import db

    now = int(time.time())
    rows = db.list_accounts(limit=5000, archived="all")
    picked = []
    for row in rows:
        if int(row.get("archived") or 0):
            continue
        acc_id = int(row.get("id") or 0)
        if acc_id in skip_ids:
            continue
        if str(row.get("live_check_status") or "") in DEAD:
            continue
        if str(row.get("downstream_rt_status") or "").strip().lower() in {"valid", "ok"}:
            continue
        # 旧账号可能没有 downstream_rt_status 字段，靠 push_status/exported_at 判断。
        if (
            str(row.get("push_status") or "") in {"pushed", "success"}
            or str(row.get("exported_at") or "").strip()
        ) and not str(row.get("downstream_rt_status") or "").strip():
            continue
        if not str(row.get("password") or "").strip() and not str(row.get("totp_secret") or "").strip():
            continue
        exp = at_exp(str(row.get("access_token") or ""))
        if not exp:
            continue
        if exp - now > days * 86400:
            continue
        picked.append({"id": acc_id, "email": str(row.get("email") or ""), "exp": exp})
    picked.sort(key=lambda r: r["exp"])
    return picked[:limit] if limit else picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = _load_state()
    fails = state.setdefault("fails", {})
    skip = {int(k) for k, v in fails.items() if int(v.get("count") or 0) >= MAX_FAILS}
    todo = candidates(args.days, args.limit, skip)
    print(f"待保活 {len(todo)} 个（{args.days} 天内到期，跳过 {len(skip)} 个已判坏）")
    if args.dry_run:
        for item in todo[:10]:
            print("   ", item["id"], item["email"], time.strftime("%m-%d %H:%M", time.localtime(item["exp"])))
        return 0
    if not todo:
        return 0

    from core import db
    from core.chatgpt2api_push import push_account
    from core.oauth_refresh import refresh_account_credentials

    stats = {"rt": 0, "dead": 0, "pushed": 0, "push_fail": 0}
    for i, item in enumerate(todo, 1):
        acc_id, email = item["id"], item["email"]
        res = refresh_account_credentials(email, write_back=True)
        if not res.get("ok"):
            if str(res.get("status") or "") == "downstream_managed":
                print(f"[{i}/{len(todo)}] {acc_id} {email[:32]:<32} 跳过（下游持有 RT）")
                time.sleep(args.sleep)
                continue
            rec = fails.setdefault(str(acc_id), {"count": 0})
            rec["count"] = int(rec.get("count") or 0) + 1
            rec["last_error"] = str(res.get("error") or res.get("status") or "")[:120]
            rec["at"] = datetime.now().isoformat(timespec="seconds")
            stats["dead"] += 1
            print(f"[{i}/{len(todo)}] {acc_id} {email[:32]:<32} RT失败 {rec['last_error'][:60]}")
            _save_state(state)
            time.sleep(args.sleep)
            continue
        fails.pop(str(acc_id), None)
        stats["rt"] += 1
        fresh = db.get_account(acc_id) or {}
        if fresh.get("access_token"):
            db.update_account_liveness(acc_id, {
                "ok": True, "status": "live",
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "access_token": str(fresh.get("access_token") or ""),
                "session": {}, "method": "keepalive_rt",
            })
        tag = "rt"
        if not args.no_push:
            pushed = push_account(acc_id)
            tag = f"rt+{pushed.get('status')}"
            if pushed.get("ok"):
                stats["pushed"] += 1
            else:
                stats["push_fail"] += 1
        print(f"[{i}/{len(todo)}] {acc_id} {email[:32]:<32} {tag}")
        _save_state(state)
        time.sleep(args.sleep)

    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    _save_state(state)
    print("完成：续期 %d | 失败 %d | 回推 %d | 推送失败 %d"
          % (stats["rt"], stats["dead"], stats["pushed"], stats["push_fail"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
