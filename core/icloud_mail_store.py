# -*- coding: utf-8 -*-
"""iCloud 隐藏邮箱池的 SQLite 存储，替代 data/icloud_mailboxes.txt + .json。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS icloud_mailboxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available',
    reason TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_icloud_mailboxes_status ON icloud_mailboxes(status, id);
CREATE TABLE IF NOT EXISTS icloud_mail_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _legacy_rows(txt: Path | None, state: Path | None) -> list[tuple]:
    """旧 TXT 提供顺序与标签，旧 JSON 提供状态；两者取并集。"""
    labels: dict[str, str] = {}
    if txt and txt.is_file():
        for line in txt.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            address, _, label = line.strip().partition("----")
            address = address.strip().lower()
            if "@" in address and address not in labels:
                labels[address] = label.strip()
    states: dict[str, dict[str, Any]] = {}
    if state and state.is_file():
        try:
            loaded = json.loads(state.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                states = {str(k).lower(): (v if isinstance(v, dict) else {}) for k, v in loaded.items()}
        except Exception:
            states = {}
    order = list(labels) + [e for e in states if e not in labels]
    rows = []
    for email in order:
        current = states.get(email) or {}
        rows.append((
            email,
            str(current.get("label") or labels.get(email) or ""),
            str(current.get("state") or "available").strip().lower() or "available",
            str(current.get("reason") or ""),
            str(current.get("imported_at") or ""),
            str(current.get("updated_at") or ""),
        ))
    return rows


def ensure(path: Path, legacy_txt: Path | None = None, legacy_state: Path | None = None) -> None:
    """建表并在首次调用时把旧 TXT/JSON 一次性迁入；之后不再读写旧文件。"""
    with closing(_conn(path)) as conn, conn:
        conn.executescript(_SCHEMA)
        if conn.execute("SELECT 1 FROM icloud_mail_meta WHERE key='migrated'").fetchone():
            return
        for row in _legacy_rows(legacy_txt, legacy_state):
            conn.execute(
                "INSERT OR IGNORE INTO icloud_mailboxes(email,label,status,reason,imported_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                row,
            )
        conn.execute("INSERT OR REPLACE INTO icloud_mail_meta(key,value) VALUES('migrated',?)", (_now(),))


def _row(item: sqlite3.Row) -> dict[str, Any]:
    email = str(item["email"])
    label = str(item["label"] or "")
    status = str(item["status"] or "available")
    updated_at = str(item["updated_at"] or "")
    return {
        "email": email,
        "label": label,
        "status": status,
        "note": str(item["reason"] or ""),
        "copy_line": f"{email}----{label}" if label else email,
        "imported_at": str(item["imported_at"] or ""),
        "created_at": str(item["imported_at"] or ""),
        "used_at": updated_at if status == "used" else "",
        "updated_at": updated_at,
        "access_token": "",
    }


def list_rows(path: Path, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    ensure(path)
    wanted = str(status or "").strip().lower()
    with closing(_conn(path)) as conn:
        if wanted:
            rows = conn.execute(
                "SELECT * FROM icloud_mailboxes WHERE status=? ORDER BY id LIMIT ?", (wanted, max(0, int(limit)))
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM icloud_mailboxes ORDER BY id LIMIT ?", (max(0, int(limit)),)).fetchall()
    return [_row(r) for r in rows]


def summary(path: Path) -> dict[str, int]:
    ensure(path)
    with closing(_conn(path)) as conn:
        counts = {str(r["status"]): int(r["n"]) for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM icloud_mailboxes GROUP BY status"
        )}
    out = {"total": sum(counts.values()), "available": 0, "in_use": 0, "used": 0, "failed": 0, "disabled": 0}
    for key, value in counts.items():
        out[key or "available"] = value
    return out


def upsert(path: Path, entries: list[dict[str, str]], overwrite: bool = False) -> tuple[int, int]:
    """追加或覆盖别名；覆盖时只把 failed 复活为 available，used/disabled 不动。"""
    ensure(path)
    inserted = updated = 0
    now = _now()
    with closing(_conn(path)) as conn, conn:
        for item in entries:
            email = str(item.get("email") or "").strip().lower()
            if "@" not in email:
                continue
            label = str(item.get("label") or "").strip()
            current = conn.execute("SELECT id,label,status FROM icloud_mailboxes WHERE email=?", (email,)).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO icloud_mailboxes(email,label,status,reason,imported_at,updated_at) "
                    "VALUES(?,?,?,'',?,?)",
                    (email, label, "available", now, now),
                )
                inserted += 1
                continue
            if not overwrite:
                continue
            if label and label != str(current["label"] or ""):
                conn.execute("UPDATE icloud_mailboxes SET label=? WHERE id=?", (label, current["id"]))
            if str(current["status"] or "").lower() == "failed":
                conn.execute(
                    "UPDATE icloud_mailboxes SET status='available', reason='', updated_at=? WHERE id=?",
                    (now, current["id"]),
                )
            updated += 1
    return inserted, updated


def mark_registered(path: Path, addresses: list[str]) -> dict[str, int]:
    """已注册账号回填 used；disabled 永不被覆盖。"""
    ensure(path)
    now = _now()
    stats = {"inserted": 0, "marked_used": 0, "already_used": 0, "preserved_disabled": 0}
    with closing(_conn(path)) as conn, conn:
        for email in addresses:
            current = conn.execute("SELECT id,status FROM icloud_mailboxes WHERE email=?", (email,)).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO icloud_mailboxes(email,label,status,reason,imported_at,updated_at) "
                    "VALUES(?,'','used','已有注册账号回填',?,?)",
                    (email, now, now),
                )
                stats["inserted"] += 1
                continue
            status = str(current["status"] or "").lower()
            if status == "disabled":
                stats["preserved_disabled"] += 1
            elif status == "used":
                stats["already_used"] += 1
            else:
                conn.execute(
                    "UPDATE icloud_mailboxes SET status='used', reason='已有注册账号回填', updated_at=? WHERE id=?",
                    (now, current["id"]),
                )
                stats["marked_used"] += 1
    return stats


def set_status(path: Path, email: str, status: str, note: str | None = None) -> bool:
    ensure(path)
    target = str(email or "").strip().lower()
    with closing(_conn(path)) as conn, conn:
        cur = conn.execute(
            "UPDATE icloud_mailboxes SET status=?, reason=?, updated_at=? WHERE email=?",
            (status, str(note or "")[:300], _now(), target),
        )
        return cur.rowcount > 0


def delete(path: Path, email: str) -> bool:
    ensure(path)
    with closing(_conn(path)) as conn, conn:
        cur = conn.execute("DELETE FROM icloud_mailboxes WHERE email=?", (str(email or "").strip().lower(),))
        return cur.rowcount > 0


def claim(path: Path) -> str | None:
    """原子领取一个可用别名；in_use 超过 1 小时视为可回收。"""
    ensure(path)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with closing(_conn(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT email FROM icloud_mailboxes WHERE status IN ('', 'available') "
                "OR (status='in_use' AND updated_at < ?) ORDER BY RANDOM() LIMIT 1",
                (cutoff,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE icloud_mailboxes SET status='in_use', reason='', updated_at=? WHERE email=?",
                (_now(), row["email"]),
            )
            conn.commit()
            return str(row["email"])
        except Exception:
            conn.rollback()
            raise


def _update_state(path: Path, email: str, status: str, reason: str, *, only_if: str | None = None) -> None:
    ensure(path)
    target = str(email or "").strip().lower()
    with closing(_conn(path)) as conn, conn:
        if only_if:
            conn.execute(
                "UPDATE icloud_mailboxes SET status=?, reason=?, updated_at=? WHERE email=? AND lower(status)=?",
                (status, reason[:300], _now(), target, only_if),
            )
        else:
            conn.execute(
                "UPDATE icloud_mailboxes SET status=?, reason=?, updated_at=? WHERE email=? AND lower(status)<>'disabled'",
                (status, reason[:300], _now(), target),
            )


def finish(path: Path, email: str, success: bool, error: str | None = None) -> None:
    _update_state(path, email, "used" if success else "failed", "" if success else str(error or ""))


def release(path: Path, email: str) -> None:
    _update_state(path, email, "available", "", only_if="in_use")


def reset(path: Path) -> None:
    ensure(path)
    with closing(_conn(path)) as conn, conn:
        conn.execute("UPDATE icloud_mailboxes SET status='available', reason='', updated_at=?", (_now(),))


def get(path: Path, email: str) -> dict[str, Any] | None:
    ensure(path)
    with closing(_conn(path)) as conn:
        row = conn.execute("SELECT * FROM icloud_mailboxes WHERE email=?", (str(email or "").strip().lower(),)).fetchone()
    return _row(row) if row else None
