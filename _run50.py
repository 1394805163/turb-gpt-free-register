# -*- coding: utf-8 -*-
"""50 号低强度注册编排（7H 窗口 / 浏览器驱动 / 每 IP 每小时 ≤10 / 注册后自动 补密码→换AT→仅AT推送）"""
import json, os, random, re, subprocess, sys, time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

TARGET = 50
WINDOW_HOURS = 7.0
# 2026-09-24 03:30 由 8 提到 10：主人定的硬线是「单 IP ≤10/小时」，我们一号一出口 IP，
# 抬全局上限不碰这条线；池子清掉 85 个已消耗别名后失败率下降，8/h 已成瓶颈。
MAX_PER_HOUR = 10

# 注册子进程最长存活；超时按已完成结果继续判定（账号可能已经建好）。
SUB_TIMEOUT = 300
MAX_PER_IP_PER_HOUR = 10
GAP_RANGE = (60, 150)
MIN_AGE_MIN = 15
DRIVER = "cloak"
STATE = "_run50.state.json"
LOG = "_run50.log"


def log(msg: str) -> None:
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_state() -> dict:
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {"started_at": datetime.now().isoformat(timespec="seconds"), "ok": 0, "attempts": 0, "records": []}


def save_state(st: dict) -> None:
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def hour_key(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%dT%H")


def reg_done_in_hour(st: dict, hk: str) -> int:
    return sum(1 for r in st["records"] if str(r.get("registered_at") or "")[:13] == hk)


def ip_done_in_hour(st: dict, ip: str, hk: str) -> int:
    return sum(1 for r in st["records"] if r.get("ip") == ip and str(r.get("registered_at") or "")[:13] == hk)


# ---------------- 注册 ----------------
def launch_registration() -> tuple[bool, dict | None]:
    before = {a.get("id") for a in _accounts()}
    env = {**os.environ, "REGISTRATION_DRIVER": DRIVER, "ENABLE_2FA": "True"}
    with open("_run50.reg.log", "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now():%H:%M:%S} 开始一次注册 =====\n")
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "main.py", "-n", "1", "--continue-on-fail"],
                                env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=SUB_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"⚠ 注册子进程超过 {SUB_TIMEOUT}s 未退出（常见于 2FA 等验证码的尾巴），强制结束并继续")
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    new = [a for a in _accounts() if a.get("id") not in before and str(a.get("created_at") or "")[:10] == datetime.now().strftime("%Y-%m-%d")]
    return (bool(new), new[0] if new else None)


def _accounts() -> list[dict]:
    from core import db
    return db.list_accounts()


def account_exit(acc: dict) -> dict:
    ex = acc.get("extra_json")
    try:
        ex = json.loads(ex) if isinstance(ex, str) else (ex or {})
    except Exception:
        ex = {}
    cb = (ex.get("cloakbrowser") or {}).get("open_result") or {}
    geo = (cb.get("locale") or {}).get("geo") or {}
    return {
        "ip": str(cb.get("proxy_exit_ip") or geo.get("ip") or ""),
        "node": str(cb.get("proxy_node") or ""),
        "country": str(geo.get("country") or ex.get("proxy_exit_country") or ""),
        "proxy": str(cb.get("proxy") or "")[:80],
    }


# ---------------- 后处理链 ----------------
def sweep_pool_leases() -> int:
    """回收卡在 in_use 但没有对应账号的邮箱（注册失败泄漏的租约）。"""
    from core import db
    from core.icloud_mail_client import set_mailbox_status
    pool_path = "data/icloud_mailboxes.json"
    try:
        pool = json.load(open(pool_path, encoding="utf-8"))
    except Exception:
        return 0
    fixed = 0
    for email, item in list(pool.items()):
        if str((item or {}).get("state") or "") != "in_use":
            continue
        if db.get_account_by_email(email):
            continue
        set_mailbox_status(email, "available", note="注册未完成泄漏的租约，自动归还")
        fixed += 1
        log(f"回收泄漏租约 → available：{email}")
    return fixed


def post_process(acc: dict) -> dict:
    from core import db
    from core.account_password import add_password_protocol
    from core.password_login import login_with_password
    from core.chatgpt2api_push import push_account, _account_payload
    from core.live_check_service import _young_account_country_hint

    email = str(acc.get("email") or "")
    acc_id = int(acc.get("id") or 0)
    db.set_account_password_pending(email, status="in_progress", reason="run50 后处理")
    out = {"id": acc_id, "email": email}

    freshest = db.get_account_by_email(email) or {}
    if not str(freshest.get("password") or "").strip():
        r = add_password_protocol(email)
        out["password"] = r.get("status")
        if not r.get("ok"):
            db.set_account_password_pending(email, status="pending", reason=f"补密码失败：{str(r.get('error') or '')[:80]}")
            return {**out, "ok": False, "step": "add_password", "error": str(r.get("error") or "")[:120]}
        log(f"  [{acc_id}] 补密码 OK")
    else:
        out["password"] = "existing"

    freshest = db.get_account_by_email(email) or {}
    pw = str(freshest.get("password") or "")
    totp = str(freshest.get("totp_secret") or "")
    hint = ""
    try:
        hint = _young_account_country_hint(acc_id) or ""
    except Exception:
        pass
    res = login_with_password(email, pw, totp_secret=totp, country_hint=hint, write_back=False, timeout=30)
    at_ok = bool(res.get("ok"))
    at_err = str(res.get("error") or "")[:120]
    if at_ok:
        db.update_account_liveness(acc_id, {"ok": True, "status": "live", "method": "password_login_at_only",
                                           "access_token": str(res.get("access_token") or ""),
                                           "checked_at": datetime.now().isoformat(timespec="seconds")})
        log(f"  [{acc_id}] 换 AT OK ({(res.get('elapsed_ms') or 0)/1000:.1f}s)")
    else:
        # 没有 2FA 的号（注册期 2FA 失败）走密码登录会 409，此时硬卡在换 AT 会让这号永远推不出去。
        # 推送本来就是"只推 AT"，所以先用注册时的 AT 推出去，换 AT 记在 reason 里等重试。
        log(f"  [{acc_id}] 换 AT 失败（{at_err[:60]}）→ 先按现有 AT 推送，换 AT 记待重试")

    push = push_account(acc_id)
    payload = _account_payload(db.get_account(acc_id) or {})
    out.update({"push": push.get("status"), "kind": payload.get("credential_kind"), "has_rt": bool(payload.get("refresh_token"))})
    if push.get("status") == "pushed":
        db.set_account_password_pending(
            email, status="done",
            reason=("补密码+换AT+推送达标" if at_ok else f"补密码+推送达标（换AT失败待重试：{at_err[:60]}）"),
        )
        log(f"  [{acc_id}] 推送 OK kind={out['kind']} 含RT={out['has_rt']}")
        out["ok"] = True
    else:
        db.set_account_password_pending(email, status="pending", reason=f"推送失败：{push.get('status')}")
        out.update({"ok": False, "step": "push", "error": str(push.get("error") or push.get("status"))[:120]})
    return out


def main() -> None:
    st = load_state()
    started = datetime.fromisoformat(st["started_at"])
    # 启动恢复：把上一轮崩溃遗留的 in_progress（超过 6 小时）退回 pending，交给扫描器或本轮处理
    try:
        from core import db as _db0

        for _a in _db0.list_accounts():
            if str(_a.get("password_pending_status") or "") != "in_progress":
                continue
            try:
                _at = datetime.fromisoformat(str(_a.get("password_pending_at") or ""))
            except Exception:
                continue
            if datetime.now() - _at > timedelta(hours=6):
                _db0.set_account_password_pending(_a.get("email"), status="pending", reason="启动恢复：重置遗留 in_progress")
    except Exception as _exc:
        log(f"启动恢复异常：{type(_exc).__name__}")
    log(f"===== run50 启动：目标 {TARGET} 个，窗口 {WINDOW_HOURS}H，驱动 {DRIVER}，"
        f"上限 {MAX_PER_HOUR}/小时、单 IP {MAX_PER_IP_PER_HOUR}/小时 =====")
    while True:
        now = datetime.now()
        if (now - started) > timedelta(hours=WINDOW_HOURS):
            log(f"窗口到期：成功 {st['ok']}/{TARGET}，退出")
            break
        if st["ok"] >= TARGET:
            log(f"目标达成：{st['ok']}/{TARGET}")
            break

        # 1) 后处理（满 15 分钟的号）
        from core import db
        for acc in db.list_accounts():
            if str(acc.get("email_source") or "") != "icloud":
                continue
            try:
                created = datetime.fromisoformat(str(acc.get("created_at") or ""))
            except Exception:
                continue
            if created < started or now - created < timedelta(minutes=MIN_AGE_MIN):
                continue
            if str(acc.get("password_pending_status") or "") == "done":
                continue
            if str(acc.get("push_status") or "") == "pushed" and acc.get("password"):
                db.set_account_password_pending(acc.get("email"), status="done", reason="已完成")
                continue
            res = post_process(acc)
            if res.get("ok"):
                st["ok"] += 1
                save_state(st)
            else:
                log(f"后处理未完成 [{res.get('id')}] step={res.get('step')} {res.get('error')}")

        # 2) 频率闸门
        hk = hour_key()
        used_hour = reg_done_in_hour(st, hk)
        if used_hour >= MAX_PER_HOUR:
            log(f"本小时已注册 {used_hour} 个（上限 {MAX_PER_HOUR}），等到下一小时")
            time.sleep(300)
            continue

        # 3) 注册一个
        ok, acc = launch_registration()
        st["attempts"] += 1
        if ok and acc:
            exit_info = account_exit(acc)
            if exit_info["ip"] and ip_done_in_hour(st, exit_info["ip"], hk) >= MAX_PER_IP_PER_HOUR:
                log(f"⚠ 出口 {exit_info['ip']} 本小时已达 {MAX_PER_IP_PER_HOUR} 个，暂停 10 分钟")
                time.sleep(600)
            st["records"].append({"id": acc.get("id"), "email": acc.get("email"),
                                  "registered_at": datetime.now().isoformat(timespec="seconds"),
                                  **exit_info})
            try:
                from core import db as _db
                # 立刻标记 in_progress：UI 侧的自动补密码扫描器只捡 pending，避免两边同时补
                _db.set_account_password_pending(acc.get("email"), status="in_progress", reason="run50 排队待后处理")
            except Exception as _exc:
                log(f"标记待后处理失败：{type(_exc).__name__}")
            log(f"注册成功 #{st['ok'] + 1} [{acc.get('id')}] {acc.get('email')} "
                f"| 出口 ip={exit_info['ip'] or '?'} node={exit_info['node'] or '?'} country={exit_info['country'] or '?'} "
                f"| 本小时 {used_hour + 1}/{MAX_PER_HOUR}")
        else:
            log(f"注册未成功（累计尝试 {st['attempts']}），等 3 分钟再来")
            time.sleep(180)
        save_state(st)

        # 4) 池子卫生 + 人类节奏间隔
        try:
            closed = sweep_pool_leases()
            if closed:
                log(f"回收 {closed} 个泄漏租约")
        except Exception as exc:
            log(f"租约回收异常：{type(exc).__name__}: {str(exc)[:90]}")
        gap = random.randint(*GAP_RANGE)
        log(f"间隔 {gap}s 后继续")
        time.sleep(gap)

    log(f"===== run50 结束：成功 {st['ok']}/{TARGET}，注册尝试 {st['attempts']} 次 =====")


if __name__ == "__main__":
    main()
