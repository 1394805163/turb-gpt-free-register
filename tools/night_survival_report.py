# -*- coding: utf-8 -*-
"""隔夜批次存活率报告工具（07:40 自动汇报用）。

流程：读流水线状态 → 对全部账号批量入队 AT 查套餐（WebUI API）→ 轮询 DB 直到完成/超时
→ 汇总（存活率 / 2FA 进度 / 补密码进度 / 流水线阶段）→ 打印 + 写 run/night-report-<date>.json

用法：
  python tools/night_survival_report.py                # 全流程（触发查套餐 + 等待 + 汇总）
  python tools/night_survival_report.py --no-enqueue   # 只读现状汇总
  python tools/night_survival_report.py --wait 480     # 自定义等待秒数（默认 660）
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

STATE_FILE = os.path.join("data", "overnight_pipeline_state.json")
AUTH = os.environ.get("WEBUI_AUTH_CODE", "73612684")
BASE = "http://127.0.0.1:5000"


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _account_rows(state: dict) -> list[dict]:
    from core import db

    rows = []
    for item in state.get("accounts") or []:
        email = str(item.get("email") or "").strip()
        if not email:
            continue
        acc = db.get_account_by_email(email) or {}
        rows.append({
            "email": email,
            "id": int(acc.get("id") or item.get("account_id") or 0),
            "password_ok": bool(item.get("password_ok")),
            "twofa_ok": bool(item.get("twofa_ok")),
            "plan_check_status": str(acc.get("plan_check_status") or ""),
            "plan_type": str(acc.get("current_plan_type") or acc.get("plan_type") or ""),
        })
    return rows


def _enqueue_all(rows: list[dict]) -> dict:
    import requests

    ids = sorted({r["id"] for r in rows if r["id"]})
    if not ids:
        return {"ok": False, "error": "没有可查询的账号"}
    resp = requests.post(
        f"{BASE}/api/accounts/check-plan-bulk",
        json={"account_ids": ids, "timezone_offset_min": "-"},
        headers={"X-Auth-Code": AUTH},
        timeout=60,
    )
    try:
        return resp.json()
    except Exception:
        return {"ok": False, "http": resp.status_code}


def _poll(rows: list[dict], wait_seconds: float) -> int:
    from core import db

    deadline = time.monotonic() + max(30.0, float(wait_seconds))
    pending_statuses = {"", "pending", "queued", "running"}
    while time.monotonic() < deadline:
        waiting = 0
        for r in rows:
            acc = db.get_account_by_email(r["email"]) or {}
            r["plan_check_status"] = str(acc.get("plan_check_status") or "")
            r["plan_type"] = str(acc.get("current_plan_type") or acc.get("plan_type") or "")
            if r["plan_check_status"] in pending_statuses:
                waiting += 1
        if waiting == 0:
            return 0
        time.sleep(15)
    return sum(1 for r in rows if r["plan_check_status"] in pending_statuses)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-enqueue", action="store_true", help="只汇总不触发查询")
    ap.add_argument("--wait", type=float, default=660.0, help="等待查询完成的秒数")
    args = ap.parse_args()

    state = _load_state()
    rows = _account_rows(state)
    enqueue_result = None
    if not args.no_enqueue and rows:
        try:
            enqueue_result = _enqueue_all(rows)
        except Exception as exc:
            enqueue_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    pending = _poll(rows, args.wait) if not args.no_enqueue else 0

    alive = sum(1 for r in rows if r["plan_check_status"] == "success")
    dead = sum(1 for r in rows if r["plan_check_status"] == "failed")
    unknown = len(rows) - alive - dead
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pipeline": {
            "status": state.get("status"),
            "phase": state.get("phase"),
            "attempts": state.get("attempts"),
            "successes": state.get("successes"),
            "target_success": state.get("target_success"),
            "batch_index": state.get("batch_index"),
            "batches": state.get("batches"),
            "liveness_done": state.get("liveness_done"),
        },
        "accounts_total": len(rows),
        "alive": alive,
        "dead": dead,
        "unknown": unknown,
        "still_pending": pending,
        "survival_rate": round(alive / len(rows), 4) if rows else None,
        "password_ok": sum(1 for r in rows if r["password_ok"]),
        "twofa_ok": sum(1 for r in rows if r["twofa_ok"]),
        "enqueue": enqueue_result,
        "accounts": rows,
    }
    out = os.path.join("run", f"night-report-{datetime.now().strftime('%Y%m%d')}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "accounts"}, ensure_ascii=False, indent=2))
    print(f"报告文件: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
