# -*- coding: utf-8 -*-
"""滚动查活：每次只挑几个"最久没查"的账号入队，交给 WebUI 后台协议查活。

查活 = 账密 + 2FA 重新登录，拿到新 AT 并回推下游（AT-only 模式下这是唯一续期手段）。
单号 7~30 秒且要吃一封 OTP 邮件，所以按"少量多次"跑：每次几个，隔几小时才轮到同一个号。

跳过规则（不碰下游凭据、不打断注册）：
  - 已归档 / 已判死
  - 下游自己持有有效 RT（再登录会转掉 RT，把下游打死）
  - 正在查活（queued/running）
  - 刚注册 < 60 分钟（还在风控观察期）
  - 距上次查活不足 --min-age-hours

用法：
    python tools/live_check_sweep.py --dry-run
    python tools/live_check_sweep.py --limit 4 --min-age-hours 6
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

DEAD = {"confirmed_dead", "deactivated"}
BUSY = {"queued", "running"}
STATE = "run/live_check_sweep.json"


def _state() -> dict:
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


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _age_hours(stamp: str) -> float:
    text = str(stamp or "").strip()
    if not text:
        return 1e9
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def candidates(limit: int, min_age_hours: float, min_registered_min: int, skip_ids: set[int]) -> list[dict]:
    from core import db

    rows = db.list_accounts(limit=5000, archived="all")
    picked = []
    for row in rows:
        if int(row.get("id") or 0) in skip_ids:
            continue
        if int(row.get("archived") or 0):
            continue
        if str(row.get("live_check_status") or "") in DEAD | BUSY:
            continue
        if str(row.get("downstream_rt_status") or "").strip().lower() in {"valid", "ok"}:
            continue
        if (
            str(row.get("push_status") or "") in {"pushed", "success"}
            or str(row.get("exported_at") or "").strip()
        ) and not str(row.get("downstream_rt_status") or "").strip():
            continue
        if not (str(row.get("password") or "").strip() or str(row.get("totp_secret") or "").strip()):
            continue
        if _age_hours(str(row.get("created_at") or "")) < min_registered_min / 60.0:
            continue
        last = _age_hours(str(row.get("live_checked_at") or ""))
        if last < min_age_hours:
            continue
        picked.append({"id": int(row.get("id") or 0), "email": str(row.get("email") or ""), "idle_h": last})
    picked.sort(key=lambda r: -r["idle_h"])
    return picked[:limit]


def _post(path: str, body: dict) -> dict:
    code = os.environ.get("WEBUI_AUTH_CODE") or _auth_code_from_env_file()
    req = urllib.request.Request(
        f"http://127.0.0.1:5000{path}",
        data=json.dumps(body).encode(),
        headers={"X-Auth-Code": code, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _auth_code_from_env_file() -> str:
    try:
        for line in open(".env", encoding="utf-8"):
            if line.startswith("WEBUI_AUTH_CODE"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--min-age-hours", type=float, default=6.0)
    ap.add_argument("--min-registered-min", type=int, default=60)
    ap.add_argument("--method", default="protocol")
    ap.add_argument("--daily-cap", type=int, default=30, help="每天最多查活多少个号（控 OTP 消耗）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = _state()
    today = _today()
    if str(state.get("date") or "") != today:
        state = {"date": today, "today_count": 0, "done": []}
    done = {int(x) for x in (state.get("done") or []) if str(x).isdigit()}
    budget = max(0, int(args.daily_cap) - int(state.get("today_count") or 0))
    limit = min(int(args.limit), budget)
    todo = candidates(limit, args.min_age_hours, args.min_registered_min, done) if limit else []
    print(f"待查活 {len(todo)} 个（limit={limit}, 今日已查 {state.get('today_count')}/{args.daily_cap}, 间隔≥{args.min_age_hours}h）")
    if args.dry_run:
        for item in todo:
            idle = "从未查活" if item["idle_h"] > 1e6 else f"空闲 {item['idle_h']:.1f}h"
            print("   ", item["id"], item["email"], idle)
        return 0
    if not todo:
        return 0
    try:
        result = _post(
            "/api/accounts/check-live-bulk",
            {"account_ids": [i["id"] for i in todo], "method": args.method},
        )
    except urllib.error.HTTPError as exc:
        print("查活入队失败 HTTP", exc.code, exc.read().decode("utf-8", "replace")[:200])
        return 1
    except Exception as exc:
        print("查活入队失败", type(exc).__name__, str(exc)[:160])
        return 1
    print("入队 %s 个 | busy %s | 失败 %s"
          % (result.get("started_count"), result.get("busy_count"), result.get("failed_count")))
    started = int(result.get("started_count") or 0)
    if started:
        state["done"] = sorted(done | {i["id"] for i in todo})
        state["today_count"] = int(state.get("today_count") or 0) + started
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
