# -*- coding: utf-8 -*-
"""隔夜注册流水线（非 CLI）：分批注册 → 逐号补密码 → 批量 2FA → 07:40 查活 → 报告。

- 批次：15+15+10+10（驱动 cloak / protocol_page 交替，出口国家 SG→JP→US→SG 轮换）
- 节奏：滚动 1 小时最多 15 次提交、提交间隔 ≥120 秒
- 注册目标 50 个有效账号，失败自动补跑（总尝试 ≤60）
- 每个账号注册成功后立即补密码（无头浏览器）
- 全部注册完成后开始批量 2FA（串行，允许跨 07:50）
- 07:40 起对全部账号做 AT 套餐查活并抽样协议重登
- 状态持久化在 data/overnight_pipeline_state.json，可断点续跑
"""
from __future__ import annotations

import json
import logging
import uuid
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _ROOT / "data" / "overnight_pipeline_state.json"
_REPORT_DIR = _ROOT / "run"
_LOCK = threading.RLock()
_THREAD: threading.Thread | None = None
_THREAD_TOKEN = ""

HOURLY_SUBMIT_CAP = 15
MIN_SUBMIT_GAP_SECONDS = 120.0
REG_JOB_WAIT_SECONDS = 1800.0
LIVENESS_POLL_SECONDS = 900.0

DEFAULT_BATCHES = [
    {"count": 15, "driver": "cloak", "country": "SG"},
    {"count": 15, "driver": "protocol_page", "country": "JP"},
    {"count": 10, "driver": "cloak", "country": "US"},
    {"count": 10, "driver": "protocol_page", "country": "SG"},
]
DEFAULT_TARGET_SUCCESS = 50
DEFAULT_MAX_ATTEMPTS = 60
DEFAULT_LIVENESS_AT = "07:40"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _load() -> dict:
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(_STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_STATE_FILE)


def _log(state: dict, message: str) -> None:
    line = f"[{_now().isoformat()}] {message}"
    logger.info("[流水线] %s", message)
    logs = state.get("log")
    if not isinstance(logs, list):
        logs = []
    logs.append(line)
    state["log"] = logs[-300:]


def _enabled() -> bool:
    with _LOCK:
        return bool(_load().get("enabled"))


def _is_runner(token: str) -> bool:
    """只有 state.active_runner 指向的线程才能提交/推进流水线。"""
    try:
        return str(_load().get("active_runner") or "") == str(token or "")
    except Exception:
        return False


def _sleep_interruptible(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if not _enabled():
            return
        time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))


def get_status() -> dict:
    with _LOCK:
        st = _load()
    if not st:
        return {"status": "idle", "phase": None, "enabled": False}
    accounts = st.get("accounts") or []
    return {
        "enabled": bool(st.get("enabled")),
        "status": st.get("status"),
        "phase": st.get("phase"),
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "batches": st.get("batches"),
        "batch_index": st.get("batch_index"),
        "batch_progress": st.get("batch_progress"),
        "attempts": st.get("attempts"),
        "successes": st.get("successes"),
        "target_success": st.get("target_success"),
        "accounts_total": len(accounts),
        "password_ok": sum(1 for a in accounts if a.get("password_ok")),
        "twofa_ok": sum(1 for a in accounts if a.get("twofa_ok")),
        "liveness_done": bool(st.get("liveness_done")),
        "last_error": st.get("last_error"),
        "report_file": st.get("report_file"),
        "log_tail": (st.get("log") or [])[-8:],
    }


def start(*, batches=None, target_success=None, max_attempts=None, liveness_at=None) -> dict:
    with _LOCK:
        st = _load()
        if st.get("enabled") and st.get("status") == "running" and _THREAD is not None and _THREAD.is_alive():
            return get_status()
        runner_token = uuid.uuid4().hex
        st = {
            "enabled": True,
            "status": "running",
            "phase": "register",
            "started_at": _now().isoformat(),
            "finished_at": None,
            "batches": batches or st.get("batches") or DEFAULT_BATCHES,
            "batch_index": int(st.get("batch_index") or 0),
            "batch_progress": int(st.get("batch_progress") or 0),
            "applied_batch": st.get("applied_batch"),
            "last_job_id": st.get("last_job_id"),
            "submitted_at": st.get("submitted_at") or [],
            "last_submit_at": 0,
            "attempts": int(st.get("attempts") or 0),
            "successes": int(st.get("successes") or 0),
            "target_success": int(target_success or st.get("target_success") or DEFAULT_TARGET_SUCCESS),
            "max_attempts": int(max_attempts or st.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
            "liveness_at": str(liveness_at or st.get("liveness_at") or DEFAULT_LIVENESS_AT),
            "liveness_done": bool(st.get("liveness_done")),
            "accounts": st.get("accounts") or [],
            "log": st.get("log") or [],
            "last_error": None,
            "active_runner": runner_token,
        }
        _log(st, "流水线启动（runner=%s）" % runner_token[:8])
        _save(st)
    _ensure_thread(runner_token)
    return get_status()


def resume() -> dict:
    return start()


def stop() -> dict:
    with _LOCK:
        st = _load()
        st["enabled"] = False
        if st.get("status") == "running":
            st["status"] = "stopped"
        _log(st, "收到停止请求")
        _save(st)
    return get_status()


def ensure_started() -> None:
    st = _load()
    if not (st.get("enabled") and st.get("status") == "running"):
        return
    token = str(st.get("active_runner") or "")
    if not token:
        token = uuid.uuid4().hex
        st["active_runner"] = token
        _save(st)
    _ensure_thread(token)


def _ensure_thread(token: str) -> None:
    global _THREAD, _THREAD_TOKEN
    with _LOCK:
        if (
            _THREAD is not None
            and _THREAD.is_alive()
            and _THREAD_TOKEN == str(token or "")
        ):
            return
        _THREAD = threading.Thread(
            target=_worker, args=(str(token or ""),), name="overnight-pipeline", daemon=True,
        )
        _THREAD_TOKEN = str(token or "")
        _THREAD.start()


def _worker(token: str) -> None:
    logger.info("[流水线] 线程启动 runner=%s", str(token or "")[:8])
    try:
        st = _load()
        if not st.get("enabled") or not _is_runner(token):
            return
        st["phase"] = "register"
        _save(st)
        _register_phase(token)
        st = _load()
        if st.get("enabled") and _is_runner(token):
            st["phase"] = "twofa"
            _save(st)
            _twofa_liveness_phase(token)
        st = _load()
        if st.get("enabled") and _is_runner(token):
            if not st.get("liveness_done"):
                st["phase"] = "liveness"
                _save(st)
                _run_liveness(token)
            _finish("completed")
        else:
            st = _load()
            if _is_runner(token):
                st["status"] = "stopped"
                _save(st)
    except Exception as exc:
        logger.exception("[流水线] 异常")
        st = _load()
        st["status"] = "failed"
        st["enabled"] = False
        st["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        _log(st, f"异常终止：{st['last_error']}")
        _save(st)


def _finish(status: str) -> None:
    st = _load()
    st["status"] = status
    st["enabled"] = False
    st["phase"] = "done"
    st["finished_at"] = _now().isoformat()
    _write_report(st)
    _save(st)
    logger.info("[流水线] 结束：%s", status)


def _write_report(state: dict) -> None:
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = _REPORT_DIR / f"overnight-report-{_now().strftime('%Y%m%d')}.json"
        payload = {
            "generated_at": _now().isoformat(),
            "status": state.get("status"),
            "phase": state.get("phase"),
            "attempts": state.get("attempts"),
            "successes": state.get("successes"),
            "target_success": state.get("target_success"),
            "accounts": state.get("accounts"),
            "liveness": state.get("liveness"),
            "log_tail": (state.get("log") or [])[-50:],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state["report_file"] = str(path)
    except Exception as exc:
        logger.warning("[流水线] 写报告失败: %s", exc)


def _apply_batch_config(batch: dict) -> None:
    driver = str(batch.get("driver") or "cloak").strip().lower()
    country = str(batch.get("country") or "").strip().upper()
    st = _load()
    try:
        from config.env_loader import write_env_values, load_env
        updates = {"REGISTRATION_DRIVER": driver}
        if country:
            updates["REGISTRATION_PROXY_ALLOWED_COUNTRIES"] = country
        write_env_values(updates)
        load_env(override=True)
        import config as _config_pkg
        _config_pkg.reload_all()
        _log(st, f"批配置已切换：driver={driver} country={country or '默认'}")
    except Exception as exc:
        _log(st, f"批配置切换失败：{type(exc).__name__}: {exc}")
    _save(st)


def _wait_submit_window(st: dict) -> None:
    while True:
        st = _load()
        if not st.get("enabled"):
            return
        now = time.time()
        raw_times = st.get("submitted_at") or []
        times = []
        for value in raw_times:
            try:
                times.append(float(value))
            except (TypeError, ValueError):
                continue
        times = sorted(t for t in times if now - t < 3600.0)
        if len(times) != len(raw_times):
            st["submitted_at"] = times
            _save(st)
        wait = 0.0
        if len(times) >= HOURLY_SUBMIT_CAP:
            wait = max(wait, times[0] + 3600.0 + 3.0 - now)
        last = float(st.get("last_submit_at") or 0)
        if last > 0:
            wait = max(wait, last + MIN_SUBMIT_GAP_SECONDS - now)
        if wait <= 0:
            return
        _sleep_interruptible(min(wait, 300.0))


def _register_phase(token: str) -> None:
    _resume_pending_job(token)
    while True:
        st = _load()
        if not st.get("enabled") or not _is_runner(token):
            return
        if int(st.get("successes") or 0) >= int(st.get("target_success") or DEFAULT_TARGET_SUCCESS):
            _log(st, f"注册阶段完成：{st.get('successes')} 个有效账号")
            _save(st)
            return
        if int(st.get("attempts") or 0) >= int(st.get("max_attempts") or DEFAULT_MAX_ATTEMPTS):
            _log(st, f"达到最大尝试次数（{st.get('attempts')}），注册阶段收尾")
            _save(st)
            return
        batches = st.get("batches") or DEFAULT_BATCHES
        bi = int(st.get("batch_index") or 0)
        topup = bi >= len(batches)
        if topup:
            attempt_no = int(st.get("attempts") or 0)
            batch = {
                "count": 1,
                "driver": "protocol_page" if attempt_no % 2 else "cloak",
                # 并集：mihomo 透明路由的实测出口国家可能与节点国别不一致（IPv6 出道）
                "country": ["SG,US", "JP,US", "US"][attempt_no % 3],
            }
        else:
            batch = batches[bi]
        applied = st.get("applied_batch")
        if applied is None or int(applied) != bi:
            _apply_batch_config(batch)
            st = _load()
            st["applied_batch"] = bi
            _save(st)
        _wait_submit_window(st)
        if not _enabled():
            return
        st = _load()
        if not topup and int(st.get("batch_progress") or 0) >= int(batch.get("count") or 0):
            st["batch_index"] = bi + 1
            st["batch_progress"] = 0
            _log(st, f"批次 {bi + 1} 提交完毕，进入下一批")
            _save(st)
            continue
        _register_one(batch, token)
        st = _load()
        st["batch_progress"] = int(st.get("batch_progress") or 0) + 1
        _save(st)


def _wait_job_terminal(job_id: int) -> dict | None:
    from core import db, registration_service as svc
    deadline = time.monotonic() + REG_JOB_WAIT_SECONDS
    while True:
        job = db.get_job(job_id) or {}
        status = str(job.get("status") or "")
        if status in ("success", "failed", "stopped", "cancelled"):
            return job
        if not _enabled():
            try:
                svc.request_stop_job(job_id)
            except Exception:
                pass
            return None
        if time.monotonic() > deadline:
            _log(_load(), f"任务 #{job_id} 等待超时，请求停止")
            try:
                svc.request_stop_job(job_id)
            except Exception:
                pass
            return db.get_job(job_id) or job
        time.sleep(5)


def _finalize_registration_job(job_id: int, job: dict) -> None:
    from core import db
    email = str(job.get("email") or "").strip()
    account = db.get_account_by_email(email) if email else None
    if account is None and job.get("account_id"):
        try:
            account = db.get_account(int(job["account_id"]))
        except (TypeError, ValueError):
            account = None
    token = ""
    if account:
        token = str(account.get("chatgpt_oauth_access_token") or account.get("access_token") or "").strip()
        email = str(account.get("email") or email)
    st = _load()
    recorded = {str(a.get("email") or "") for a in st.get("accounts") or []}
    if email and email in recorded:
        if int(st.get("last_job_id") or 0) == int(job_id):
            st["last_job_id"] = None
            _save(st)
        return
    if not (email and account and token):
        _log(st, f"任务 #{job_id} 未产出有效账号（status={job.get('status')} email={email or '-'}）")
        if int(st.get("last_job_id") or 0) == int(job_id):
            st["last_job_id"] = None
        _save(st)
        return
    password_status = "failed"
    password_value = ""
    try:
        existing = str(account.get("password") or "").strip()
        if existing:
            password_status = "existing"
            password_value = existing
        else:
            from core.account_password import set_account_password
            result = set_account_password(email) or {}
            if result.get("ok"):
                password_status = "ok"
                password_value = str(result.get("password") or "")
            else:
                password_status = str(result.get("status") or "failed")
    except Exception as exc:
        password_status = f"error:{type(exc).__name__}"
    st = _load()
    st.setdefault("accounts", []).append({
        "email": email,
        "account_id": int(account.get("id") or 0),
        "job_id": job_id,
        "registered_at": _now().isoformat(),
        "password_ok": password_status in ("ok", "existing"),
        "password_status": password_status,
        "password": password_value,
        "twofa_ok": False,
        "twofa_status": "pending",
    })
    st["successes"] = int(st.get("successes") or 0) + 1
    if int(st.get("last_job_id") or 0) == int(job_id):
        st["last_job_id"] = None
    _log(st, f"账号 {email} 注册成功，补密码={password_status}")
    _save(st)


def _resume_pending_job(token: str) -> None:
    st = _load()
    if not _is_runner(token):
        return
    job_id = int(st.get("last_job_id") or 0)
    if not job_id:
        return
    from core import db
    job = db.get_job(job_id) or {}
    status = str(job.get("status") or "")
    if status in ("pending", "running", "stopping") and st.get("enabled"):
        _log(st, f"检测到未完成任务 #{job_id}，继续等待")
        _save(st)
        job = _wait_job_terminal(job_id)
    if job is None:
        return
    _finalize_registration_job(job_id, job)


def _register_one(batch: dict, token: str) -> None:
    from core import registration_service as svc
    st = _load()
    if not st.get("enabled") or not _is_runner(token):
        return
    jobs = svc.submit_registration(count=1, workers=1)
    job_id = int((jobs[0] or {}).get("id") or 0)
    st = _load()
    st.setdefault("submitted_at", []).append(time.time())
    st["last_submit_at"] = time.time()
    st["attempts"] = int(st.get("attempts") or 0) + 1
    st["last_job_id"] = job_id
    _log(st, f"提交注册任务 #{job_id}（driver={batch.get('driver')} country={batch.get('country')}）")
    _save(st)
    if not job_id:
        return
    job = _wait_job_terminal(job_id)
    if job is None:
        return
    _finalize_registration_job(job_id, job)


def _liveness_deadline(st: dict) -> datetime:
    raw = str(st.get("liveness_at") or DEFAULT_LIVENESS_AT)
    try:
        hh, mm = [int(x) for x in raw.split(":")[:2]]
    except Exception:
        hh, mm = 7, 40
    return _now().replace(hour=hh, minute=mm, second=0)


def _twofa_liveness_phase(token: str) -> None:
    while True:
        st = _load()
        if not st.get("enabled") or not _is_runner(token):
            return
        dead = _liveness_deadline(st)
        if not st.get("liveness_done") and _now() >= dead:
            st["phase"] = "liveness"
            _save(st)
            _run_liveness()
            continue
        # 单个账号 2FA 失败不能卡住整队：每个账号最多尝试 2 次后跳过。
        pending = [
            a for a in (st.get("accounts") or [])
            if not a.get("twofa_ok") and int(a.get("twofa_attempts") or 0) < 2
        ]
        if not pending:
            if st.get("liveness_done"):
                return
            wait = max(5.0, min(120.0, (dead - _now()).total_seconds()))
            _sleep_interruptible(wait)
            continue
        target = pending[0]
        email = str(target.get("email") or "")
        if not target.get("password_ok") and not target.get("password_retried"):
            retry_status = "failed"
            try:
                from core.account_password import set_account_password
                rr = set_account_password(email) or {}
                retry_status = "ok" if rr.get("ok") else str(rr.get("status") or "failed")
            except Exception as exc:
                retry_status = f"error:{type(exc).__name__}"
            st = _load()
            for a in st.get("accounts") or []:
                if a.get("email") == email:
                    a["password_retried"] = True
                    if retry_status == "ok":
                        a["password_ok"] = True
                        a["password_status"] = "ok_retry"
            _log(st, f"补密码重试 {email}: {retry_status}")
            _save(st)
            if retry_status == "license_busy":
                _sleep_interruptible(90)
            continue
        status = "failed"
        secret = ""
        try:
            from core.account_2fa import set_account_2fa
            result = set_account_2fa(email) or {}
            if result.get("ok"):
                status = "ok"
                secret = str(result.get("totp_secret") or "")
            else:
                status = str(result.get("status") or "failed")
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
        if status == "license_busy":
            _log(st, f"2FA {email}: 浏览器席位居满，退避重试（不计失败）")
            _sleep_interruptible(90)
            continue
        st = _load()
        for a in st.get("accounts") or []:
            if a.get("email") == email:
                a["twofa_ok"] = status in ("ok", "updated", "existing")
                a["twofa_status"] = status
                a["twofa_attempts"] = int(a.get("twofa_attempts") or 0) + 1
                if secret:
                    a["totp_secret"] = secret
        _log(st, f"2FA {email}: {status}")
        _save(st)


def _run_liveness(token: str = "") -> None:
    from core import db, plan_check_service, live_check_service
    st = _load()
    if token and not _is_runner(token):
        return
    accounts = list(st.get("accounts") or [])
    _log(st, f"开始查活（{len(accounts)} 个账号）")
    _save(st)
    for item in accounts:
        if not _enabled():
            return
        email = str(item.get("email") or "")
        try:
            acc = db.get_account_by_email(email) or {}
            acc_id = int(acc.get("id") or item.get("account_id") or 0)
            email = str(acc.get("email") or email)
            token = str(acc.get("chatgpt_oauth_access_token") or acc.get("access_token") or "").strip()
            if not (acc_id and email and token):
                _log(_load(), f"查活跳过 {email or acc_id}：缺少 AT")
                continue
            result = plan_check_service.enqueue_account_plan_check(
                account_id=acc_id, email=email, access_token=token, trigger="overnight",
            )
            if not result.get("accepted") and result.get("queue_full"):
                _sleep_interruptible(20.0)
        except Exception as exc:
            _log(_load(), f"查活入队失败 {email}：{type(exc).__name__}")
        _sleep_interruptible(2.0)
    for item in accounts[:3]:
        try:
            acc = db.get_account_by_email(str(item.get("email") or "")) or {}
            acc_id = int(acc.get("id") or 0)
            if acc_id:
                live_check_service.enqueue_account_live_check(
                    account_id=acc_id,
                    email=str(acc.get("email") or ""),
                    trigger="overnight-sample",
                    method="protocol",
                )
        except Exception:
            pass
    deadline = time.monotonic() + LIVENESS_POLL_SECONDS
    pending = {str(a.get("email") or "") for a in accounts}
    while pending and time.monotonic() < deadline:
        done = set()
        for email in list(pending):
            row = db.get_account_by_email(email) or {}
            if str(row.get("plan_check_status") or "") not in ("queued", "running", "pending"):
                done.add(email)
        pending -= done
        if pending:
            _sleep_interruptible(15.0)
            if not _enabled():
                break
    st = _load()
    snapshot = {}
    alive = failed = unknown = 0
    for item in accounts:
        email = str(item.get("email") or "")
        row = db.get_account_by_email(email) or {}
        status = str(row.get("plan_check_status") or "")
        if status == "success":
            alive += 1
        elif status == "failed":
            failed += 1
        else:
            unknown += 1
        snapshot[email] = {
            "plan_check_status": status,
            "current_plan_type": row.get("current_plan_type") or row.get("plan_type") or "",
            "plan_check_ok": bool(row.get("plan_check_ok")),
            "plan_check_error": str(row.get("plan_check_error") or "")[:200],
            "twofa_ok": bool(item.get("twofa_ok")),
        }
    st["liveness"] = {
        "checked_at": _now().isoformat(),
        "alive": alive,
        "failed": failed,
        "unknown": unknown,
        "pending": len(pending),
        "accounts": snapshot,
    }
    st["liveness_done"] = True
    _log(st, f"查活完成：成功 {alive} / 失败 {failed} / 未知 {unknown}（超时未完成 {len(pending)}）")
    _save(st)
