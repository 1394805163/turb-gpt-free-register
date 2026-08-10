from __future__ import annotations

import hashlib
import imaplib
import json
import re
import time
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import Any


_state_lock = Lock()


class ICloudMailboxPool:
    """仅保留 iCloud 隐藏邮箱池与 IMAP 验证码读取。"""

    def __init__(self, config: dict[str, Any], state_file: Path) -> None:
        self.config = config
        self.state_file = state_file
        self.lock = _state_lock

    @staticmethod
    def parse_entries(value: Any) -> list[dict[str, str]]:
        lines = value if isinstance(value, list) else str(value or "").splitlines()
        seen: set[str] = set()
        entries: list[dict[str, str]] = []
        for line in lines:
            address, _, label = str(line).partition("----")
            address = address.strip().lower()
            if "@" in address and address not in seen:
                seen.add(address)
                entries.append({"email": address, "label": label.strip()})
        return entries

    @classmethod
    def parse(cls, value: Any) -> list[str]:
        return [item["email"] for item in cls.parse_entries(value)]

    @staticmethod
    def serialize(entries: list[dict[str, str]]) -> str:
        return "\n".join(f'{item["email"]}----{item["label"]}' if item.get("label") else item["email"] for item in entries)

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, state: dict[str, dict[str, str]]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _available(item: dict[str, str] | None) -> bool:
        if not item:
            return True
        state = str(item.get("state") or "")
        if state in {"used", "failed", "disabled"}:
            return False
        if state != "in_use":
            return True
        try:
            updated = datetime.fromisoformat(str(item.get("updated_at") or ""))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - updated).total_seconds() >= 3600
        except Exception:
            return True

    def acquire(self) -> dict[str, Any]:
        addresses = self.parse(self.config.get("mailboxes"))
        if not addresses:
            raise RuntimeError("iCloud 隐藏邮箱池为空")
        with self.lock:
            state = self._load()
            address = next((item for item in addresses if self._available(state.get(item))), "")
            if not address:
                raise RuntimeError(f"iCloud 邮箱池暂无可用邮箱（共 {len(addresses)} 个）")
            current = dict(state.get(address) or {})
            current.update({"state": "in_use", "reason": "", "updated_at": datetime.now(timezone.utc).isoformat()})
            state[address] = current
            self._save(state)
        return {"address": address, "_code_not_before": datetime.now(timezone.utc), "_seen": set()}

    def finish(self, mailbox: dict[str, Any], success: bool, error: Exception | str | None = None) -> None:
        address = str(mailbox.get("address") or "").lower()
        if not address:
            return
        with self.lock:
            state = self._load()
            current = dict(state.get(address) or {})
            current.update({
                "state": "used" if success else "failed",
                "reason": "" if success else str(error or "")[:300],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            state[address] = current
            self._save(state)

    def release(self, mailbox: dict[str, Any]) -> None:
        address = str(mailbox.get("address") or "").lower()
        with self.lock:
            state = self._load()
            if str((state.get(address) or {}).get("state")) == "in_use":
                current = dict(state.get(address) or {})
                current.update({"state": "available", "reason": "", "updated_at": datetime.now(timezone.utc).isoformat()})
                state[address] = current
                self._save(state)

    def reset(self) -> None:
        with self.lock:
            self._save({})

    @staticmethod
    def _decode(value: Any) -> str:
        try:
            return str(make_header(decode_header(str(value or ""))))
        except Exception:
            return str(value or "")

    @staticmethod
    def _body(message) -> str:
        parts: list[str] = []
        for part in message.walk() if message.is_multipart() else [message]:
            if part.get_content_maintype() == "multipart":
                continue
            try:
                value = part.get_content()
            except Exception:
                continue
            if value and part.get_content_type().startswith("text/"):
                parts.append(str(value))
        return "\n".join(parts)

    def _connect_imap(self):
        username = str(self.config.get("imap_username") or "").strip()
        password = str(self.config.get("imap_password") or "").strip()
        if not username or not password:
            raise RuntimeError("请配置 iCloud IMAP 邮箱和 Apple 专用密码")
        imap = imaplib.IMAP4_SSL(str(self.config.get("imap_host") or "imap.mail.me.com"), int(self.config.get("imap_port") or 993), timeout=float(self.config.get("request_timeout") or 30))
        imap.login(username, password)
        status, _ = imap.select(str(self.config.get("imap_mailbox") or "INBOX"), readonly=True)
        if status != "OK":
            try:
                imap.logout()
            except Exception:
                pass
            raise RuntimeError("iCloud IMAP 打开收件箱失败")
        return imap

    def _candidate_uids(self, imap, address: str) -> list[bytes]:
        """仅返回当前隐藏邮箱的邮件；多个注册任务共用收件箱时绝不回退到 ALL。"""
        # ponytail: 当前注册流程至多会产生少量重发邮件；需要更多历史邮件时再调高上限。
        limit = min(6, max(1, int(self.config.get("message_limit") or 20)))
        candidates: set[bytes] = set()
        for header in ("TO", "DELIVERED-TO", "X-ORIGINAL-TO"):
            status, data = imap.uid("search", None, "HEADER", header, address)
            if status == "OK" and data and data[0]:
                candidates.update(data[0].split())
        return sorted(candidates, key=lambda uid: int(uid))[-limit:]

    def _messages(self, mailbox: dict[str, Any], imap=None) -> list[tuple[str, str, datetime | None, str]]:
        own_connection = imap is None
        imap = imap or self._connect_imap()
        try:
            messages: list[tuple[str, str, datetime | None, str]] = []
            target = str(mailbox["address"]).strip().lower()
            for uid in reversed(self._candidate_uids(imap, target)):
                status, fetched = imap.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((part[1] for part in fetched or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
                if status != "OK" or not raw:
                    continue
                message = message_from_bytes(raw, policy=policy.default)
                try:
                    received = parsedate_to_datetime(str(message.get("Date") or ""))
                    received = received if received.tzinfo else received.replace(tzinfo=timezone.utc)
                except Exception:
                    received = None
                text = f"{self._decode(message.get('Subject'))}\n{self._body(message)}"
                headers = "\n".join(f"{key}: {self._decode(value)}" for key, value in message.items())
                if target not in headers.lower():
                    continue
                messages.append((str(message.get("Message-ID") or uid.decode()), text, received, headers))
            return messages
        finally:
            if own_connection:
                try:
                    imap.logout()
                except Exception:
                    pass

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        deadline = time.monotonic() + float(self.config.get("wait_timeout") or 360)
        imap = None
        try:
            while time.monotonic() < deadline:
                try:
                    imap = imap or self._connect_imap()
                    for message_id, text, received, _headers in self._messages(mailbox, imap):
                        if received and received < mailbox["_code_not_before"]:
                            continue
                        fingerprint = message_id or hashlib.sha256(text.encode()).hexdigest()
                        if fingerprint in mailbox["_seen"]:
                            continue
                        mailbox["_seen"].add(fingerprint)
                        match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", text, re.I) or re.search(r"(?<![#&])\b(\d{6})\b", text)
                        if match and match.group(1) != "177010":
                            return match.group(1)
                except (imaplib.IMAP4.error, OSError):
                    try:
                        if imap:
                            imap.logout()
                    except Exception:
                        pass
                    imap = None
                time.sleep(max(1, min(2, float(self.config.get("wait_interval") or 5))))
            return None
        finally:
            try:
                if imap:
                    imap.logout()
            except Exception:
                pass
