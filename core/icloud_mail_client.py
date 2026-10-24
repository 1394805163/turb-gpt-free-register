from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import icloud_mail_store as store
from core.icloud_mail_pool import ICloudMailboxPool
from core.log_safety import redact_email


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _PROJECT_ROOT / "data" / "icloud_mailboxes.json"
_DEFAULT_DB = _PROJECT_ROOT / "turb.sqlite3"


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
    return {
        "legacy_txt": str(mailbox_file),
        "db_path": str(_db_path()),
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


def _db_path() -> Path:
    """测试通过 patch _STATE_FILE 注入独立库；正式库与账号共用 turb.sqlite3。"""
    from config import email as cfg

    legacy = _resolve_path(getattr(cfg, "ICLOUD_MAILBOXES_FILE", "data/icloud_mailboxes.txt"))
    if legacy == _PROJECT_ROOT / "data" / "icloud_mailboxes.txt":
        return _DEFAULT_DB
    return legacy.with_suffix(".sqlite3")


def _mailboxes_path() -> Path:
    from config import email as cfg

    return _resolve_path(getattr(cfg, "ICLOUD_MAILBOXES_FILE", "data/icloud_mailboxes.txt"))


def import_mailboxes(text: str, overwrite: bool = False) -> dict[str, int | bool]:
    """把隐藏邮箱别名追加到文本池，保留现有状态和标签。

    ``overwrite=True`` 时覆盖同名别名：更新标签，并把 failed 状态重置为 available；
    used / disabled / in_use 状态保持不变（避免复活已用或已停用的邮箱）。
    """
    valid_lines = []
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "@" in raw.partition("----")[0]:
            valid_lines.append(raw)
    incoming = ICloudMailboxPool.parse_entries(valid_lines)
    path = _db_path()
    pool = _pool()
    with pool.lock:
        store.ensure(path, _mailboxes_path(), _STATE_FILE)
        inserted, updated = store.upsert(path, incoming, overwrite=overwrite)
    return {
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "skipped": max(0, len(valid_lines) - inserted - updated),
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

    path = _db_path()
    pool = _pool()
    with pool.lock:
        store.ensure(path, _mailboxes_path(), _STATE_FILE)
        stats = store.mark_registered(path, addresses)
    return {
        "accounts": len(addresses),
        **stats,
    }


def list_mailboxes(status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    wanted = str(status or "").strip().lower()
    path = _db_path()
    store.ensure(path, _mailboxes_path(), _STATE_FILE)
    return store.list_rows(path, status=wanted or None, limit=limit)


def mailbox_summary() -> dict[str, int]:
    path = _db_path()
    store.ensure(path, _mailboxes_path(), _STATE_FILE)
    return store.summary(path)


def set_mailbox_status(email: str, status: str, note: str | None = None) -> None:
    target = str(email or "").strip().lower()
    normalized = str(status or "").strip().lower()
    if normalized not in {"available", "in_use", "used", "failed", "disabled"}:
        raise ValueError("iCloud 邮箱状态非法")
    path = _db_path()
    pool = _pool()
    with pool.lock:
        store.ensure(path, _mailboxes_path(), _STATE_FILE)
        if not store.set_status(path, target, normalized, note):
            raise KeyError(f"iCloud 邮箱不存在: {target}")


def delete_mailbox(email: str) -> bool:
    target = str(email or "").strip().lower()
    path = _db_path()
    pool = _pool()
    with pool.lock:
        store.ensure(path, _mailboxes_path(), _STATE_FILE)
        return store.delete(path, target)


def pick_account() -> ICloudAccount:
    mailbox = _pool().acquire()
    return ICloudAccount(email=str(mailbox["address"]))


def get_account_context(email: str) -> dict[str, Any] | None:
    target = str(email or "").strip().lower()
    if not target:
        return None
    row = store.get(_db_path(), target)
    if not row:
        return None
    return {
        "email": target,
        "state": row["status"],
        "reason": row["note"],
        "label": row["label"],
        "imported_at": row["imported_at"],
        "updated_at": row["updated_at"],
    }


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
    backfill_uids = state.setdefault("backfill_uids", set())
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
        "_backfill_uids": backfill_uids,
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
