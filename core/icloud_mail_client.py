from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.icloud_mail_pool import ICloudMailboxPool
from core.log_safety import redact_email


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _PROJECT_ROOT / "data" / "icloud_mailboxes.json"


@dataclass(frozen=True)
class ICloudAccount:
    email: str


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


def _settings() -> dict[str, Any]:
    from config import email as cfg

    mailbox_file = _resolve_path(getattr(cfg, "ICLOUD_MAILBOXES_FILE", "data/icloud_mailboxes.txt"))
    mailboxes = mailbox_file.read_text(encoding="utf-8-sig", errors="replace") if mailbox_file.is_file() else ""
    return {
        "mailboxes": mailboxes,
        "imap_host": getattr(cfg, "ICLOUD_IMAP_HOST", "imap.mail.me.com"),
        "imap_port": int(getattr(cfg, "ICLOUD_IMAP_PORT", 993) or 993),
        "imap_username": getattr(cfg, "ICLOUD_IMAP_USERNAME", ""),
        "imap_password": getattr(cfg, "ICLOUD_IMAP_PASSWORD", ""),
        "imap_mailbox": getattr(cfg, "ICLOUD_IMAP_MAILBOX", "INBOX"),
        "request_timeout": int(getattr(cfg, "ICLOUD_REQUEST_TIMEOUT", 30) or 30),
        "message_limit": int(getattr(cfg, "ICLOUD_MESSAGE_LIMIT", 6) or 6),
        "initial_scan_limit": int(getattr(cfg, "ICLOUD_INITIAL_SCAN_LIMIT", 20) or 20),
        "reselect_interval": int(getattr(cfg, "ICLOUD_RESELECT_INTERVAL", 12) or 12),
        "reconnect_interval": int(getattr(cfg, "ICLOUD_RECONNECT_INTERVAL", 18) or 18),
        "clock_skew_seconds": int(getattr(cfg, "ICLOUD_CLOCK_SKEW_SECONDS", 30) or 30),
        "wait_timeout": int(getattr(cfg, "OTP_MAX_WAIT", 120) or 120),
        "wait_interval": int(getattr(cfg, "OTP_POLL_INTERVAL", 3) or 3),
    }


def _pool(**overrides: Any) -> ICloudMailboxPool:
    settings = _settings()
    settings.update({key: value for key, value in overrides.items() if value is not None})
    return ICloudMailboxPool(settings, _STATE_FILE)


def _mailboxes_path() -> Path:
    from config import email as cfg

    return _resolve_path(getattr(cfg, "ICLOUD_MAILBOXES_FILE", "data/icloud_mailboxes.txt"))


def _write_mailboxes(entries: list[dict[str, str]]) -> None:
    path = _mailboxes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    content = ICloudMailboxPool.serialize(entries)
    temp_path.write_text(content + ("\n" if content else ""), encoding="utf-8")
    temp_path.replace(path)


def import_mailboxes(text: str) -> dict[str, int | bool]:
    """把隐藏邮箱别名追加到文本池，保留现有状态和标签。"""
    valid_lines = []
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "@" in raw.partition("----")[0]:
            valid_lines.append(raw)
    incoming = ICloudMailboxPool.parse_entries(valid_lines)
    pool = _pool()
    with pool.lock:
        existing = ICloudMailboxPool.parse_entries(pool.config.get("mailboxes"))
        known = {item["email"] for item in existing}
        added = [item for item in incoming if item["email"] not in known]
        now = datetime.now(timezone.utc).isoformat()
        if added:
            _write_mailboxes(existing + added)
            state = pool._load()
            for item in added:
                state[item["email"]] = {
                    "state": "available",
                    "reason": "",
                    "label": item.get("label") or "",
                    "imported_at": now,
                    "updated_at": now,
                }
            pool._save(state)
    inserted = len(added)
    return {
        "ok": True,
        "inserted": inserted,
        "skipped": max(0, len(valid_lines) - inserted),
        "parsed": len(valid_lines),
        "as_registered": False,
    }


def sync_registered_mailboxes(accounts: list[dict] | tuple[dict, ...]) -> dict[str, int]:
    """把已注册的 iCloud 账号回填为邮箱池 ``used`` 状态。

    注册账号是权威来源：邮箱不存在于当前池文件时补入，已有别名保持标签；
    已手动停用的别名不被自动恢复，避免把 Apple 控制台停用项重新放回可用状态。
    """
    addresses: list[str] = []
    seen: set[str] = set()
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        email = str(account.get("email") or "").strip().lower()
        source = str(account.get("email_source") or "").strip().lower()
        if source != "icloud" and email.rsplit("@", 1)[-1] not in {"icloud.com", "me.com", "mac.com"}:
            continue
        if "@" not in email or email in seen:
            continue
        seen.add(email)
        addresses.append(email)

    pool = _pool()
    with pool.lock:
        existing = ICloudMailboxPool.parse_entries(pool.config.get("mailboxes"))
        existing_by_email = {item["email"]: item for item in existing}
        state = pool._load()
        now = datetime.now(timezone.utc).isoformat()
        inserted = marked_used = already_used = preserved_disabled = 0
        state_dirty = False
        for email in addresses:
            if email not in existing_by_email:
                item = {"email": email, "label": ""}
                existing.append(item)
                existing_by_email[email] = item
                state[email] = {
                    "state": "used",
                    "reason": "已有注册账号回填",
                    "label": "",
                    "imported_at": now,
                    "updated_at": now,
                }
                inserted += 1
                state_dirty = True
                continue
            current = dict(state.get(email) or {})
            current_state = str(current.get("state") or "available").strip().lower()
            if current_state == "disabled":
                preserved_disabled += 1
                continue
            if current_state == "used":
                already_used += 1
                continue
            current.update({
                "state": "used",
                "reason": "已有注册账号回填",
                "label": existing_by_email[email].get("label") or current.get("label") or "",
                "updated_at": now,
            })
            state[email] = current
            marked_used += 1
            state_dirty = True
        if inserted:
            _write_mailboxes(existing)
        if state_dirty:
            pool._save(state)
    return {
        "accounts": len(addresses),
        "inserted": inserted,
        "marked_used": marked_used,
        "already_used": already_used,
        "preserved_disabled": preserved_disabled,
    }


def list_mailboxes(status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    pool = _pool()
    entries = ICloudMailboxPool.parse_entries(pool.config.get("mailboxes"))
    with pool.lock:
        state = pool._load()
    rows: list[dict[str, Any]] = []
    wanted = str(status or "").strip().lower()
    for item in entries:
        email = item["email"]
        current = dict(state.get(email) or {})
        current_status = str(current.get("state") or "available").strip().lower()
        if wanted and current_status != wanted:
            continue
        label = item.get("label") or str(current.get("label") or "")
        updated_at = str(current.get("updated_at") or "")
        rows.append({
            "email": email,
            "label": label,
            "status": current_status,
            "note": str(current.get("reason") or ""),
            "copy_line": f"{email}----{label}" if label else email,
            "imported_at": str(current.get("imported_at") or ""),
            "created_at": str(current.get("imported_at") or ""),
            "used_at": updated_at if current_status == "used" else "",
            "updated_at": updated_at,
            "access_token": "",
        })
    return rows[: max(0, int(limit))]


def mailbox_summary() -> dict[str, int]:
    rows = list_mailboxes(limit=1_000_000)
    result = {"total": len(rows), "available": 0, "in_use": 0, "used": 0, "failed": 0, "disabled": 0}
    for row in rows:
        key = str(row.get("status") or "available")
        result[key] = int(result.get(key, 0)) + 1
    return result


def set_mailbox_status(email: str, status: str, note: str | None = None) -> None:
    target = str(email or "").strip().lower()
    normalized = str(status or "").strip().lower()
    if normalized not in {"available", "in_use", "used", "failed", "disabled"}:
        raise ValueError("iCloud 邮箱状态非法")
    pool = _pool()
    entries = ICloudMailboxPool.parse_entries(pool.config.get("mailboxes"))
    matched = next((item for item in entries if item["email"] == target), None)
    if not matched:
        raise KeyError(f"iCloud 邮箱不存在: {target}")
    with pool.lock:
        state = pool._load()
        current = dict(state.get(target) or {})
        current.update({
            "state": normalized,
            "reason": str(note or "")[:300],
            "label": matched.get("label") or current.get("label") or "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        state[target] = current
        pool._save(state)


def delete_mailbox(email: str) -> bool:
    target = str(email or "").strip().lower()
    pool = _pool()
    with pool.lock:
        entries = ICloudMailboxPool.parse_entries(pool.config.get("mailboxes"))
        kept = [item for item in entries if item["email"] != target]
        if len(kept) == len(entries):
            return False
        _write_mailboxes(kept)
        state = pool._load()
        state.pop(target, None)
        pool._save(state)
    return True


def pick_account() -> ICloudAccount:
    mailbox = _pool().acquire()
    return ICloudAccount(email=str(mailbox["address"]))


def get_account_context(email: str) -> dict[str, Any] | None:
    target = str(email or "").strip().lower()
    if not target:
        return None
    pool = _pool()
    if target not in pool.parse(pool.config.get("mailboxes")):
        return None
    state: dict[str, Any] = {}
    try:
        loaded = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = dict(loaded.get(target) or {})
    except Exception:
        pass
    return {"email": target, **state}


def fetch_latest_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    used_codes: set[str] | None = None,
    otp_state: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> str:
    state = otp_state if otp_state is not None else {}
    seen_uids = state.setdefault("seen_uids", set())
    pending_uids = state.setdefault("pending_uids", set())
    seen_message_ids = state.setdefault("seen_message_ids", set())
    seen_code_hashes = state.setdefault("seen_code_hashes", set())
    used_code_hashes = state.setdefault("used_code_hashes", set())
    for code in used_codes or set():
        used_code_hashes.add(hashlib.sha256(str(code).encode()).hexdigest())
    mailbox = {
        "address": str(email or "").strip().lower(),
        "_code_not_before": datetime.fromtimestamp(float(after_ts), tz=timezone.utc),
        "_seen": seen_message_ids,
        "_seen_uids": seen_uids,
        "_pending_uids": pending_uids,
        "_seen_message_ids": seen_message_ids,
        "_seen_code_hashes": seen_code_hashes,
        "_used_code_hashes": used_code_hashes,
        "_last_uid": int(state.get("last_uid") or 0),
        "_uidvalidity": str(state.get("uidvalidity") or ""),
    }
    try:
        code = _pool(wait_timeout=max_wait, wait_interval=poll_interval).wait_for_code(mailbox)
    finally:
        state["last_uid"] = int(mailbox.get("_last_uid") or 0)
        state["uidvalidity"] = str(mailbox.get("_uidvalidity") or "")
    if not code:
        raise TimeoutError(f"iCloud OTP wait timed out for {redact_email(mailbox['address'])}")
    return code


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    mailbox = {"address": str(email or "").strip().lower()}
    pool = _pool()
    normalized = str(status or "available").strip().lower()
    if normalized in {"available", "released", "retry"}:
        pool.release(mailbox)
    elif normalized in {"used", "success", "completed"}:
        pool.finish(mailbox, True)
    elif normalized == "disabled":
        # Apple 控制台手动停用别名后，本地池同步为 disabled，禁止再次领取。
        set_mailbox_status(email, "disabled", note=note or "iCloud 别名已停用")
    else:
        pool.finish(mailbox, False, note or normalized)
