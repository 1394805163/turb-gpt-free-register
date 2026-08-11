from __future__ import annotations

import hashlib
import imaplib
import json
import re
import time
from datetime import datetime, timedelta, timezone
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

    def _mailbox_name(self) -> str:
        return str(self.config.get("imap_mailbox") or "INBOX")

    @staticmethod
    def _close_imap(imap) -> None:
        if imap is None:
            return
        try:
            imap.logout()
        except Exception:
            pass

    @staticmethod
    def _uidvalidity(imap) -> str:
        try:
            _status, values = imap.response("UIDVALIDITY")
            if values and values[0] is not None:
                value = values[0]
                return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
        except Exception:
            pass
        return ""

    def _refresh_selected_mailbox(self, imap, *, force_select: bool = False) -> None:
        status, _ = imap.noop()
        if status != "OK":
            raise imaplib.IMAP4.error("iCloud IMAP NOOP 失败")
        if force_select:
            status, _ = imap.select(self._mailbox_name(), readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error("iCloud IMAP 重新选择收件箱失败")

    @staticmethod
    def _all_uids(imap) -> list[bytes]:
        status, data = imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        values = [value for value in data[0].split() if value.isdigit()]
        return sorted(values, key=lambda value: int(value))

    def _candidate_uid_window(self, all_uids: list[bytes], mailbox: dict[str, Any]) -> list[bytes]:
        seen_uids = mailbox.setdefault("_seen_uids", set())
        pending_uids = mailbox.setdefault("_pending_uids", set())
        last_uid = int(mailbox.get("_last_uid") or 0)
        initial_limit = max(1, min(100, int(self.config.get("initial_scan_limit") or 20)))
        if last_uid > 0:
            candidates = [uid for uid in all_uids if int(uid) > last_uid or uid in pending_uids]
        else:
            candidates = all_uids[-initial_limit:]
        return [uid for uid in candidates if uid not in seen_uids]

    def _parse_uid_message(self, uid: bytes, raw: bytes) -> tuple[str, str, datetime | None, str] | None:
        if not raw:
            return None
        message = message_from_bytes(raw, policy=policy.default)
        try:
            received = parsedate_to_datetime(str(message.get("Date") or ""))
            received = received if received.tzinfo else received.replace(tzinfo=timezone.utc)
        except Exception:
            received = None
        text = f"{self._decode(message.get('Subject'))}\n{self._body(message)}"
        headers = "\n".join(f"{key}: {self._decode(value)}" for key, value in message.items())
        message_id = str(message.get("Message-ID") or uid.decode(errors="replace"))
        return message_id, text, received, headers

    def _fetch_uid_message(self, imap, uid: bytes) -> tuple[str, str, datetime | None, str] | None:
        status, fetched = imap.uid("fetch", uid, "(BODY.PEEK[])")
        raw = next(
            (part[1] for part in fetched or [] if isinstance(part, tuple) and isinstance(part[1], bytes)),
            b"",
        )
        if status != "OK" or not raw:
            return None
        return self._parse_uid_message(uid, raw)

    def _candidate_uids(self, imap, address: str) -> list[bytes]:
        """兼容旧调用：枚举末尾 UID，别名匹配统一在下载邮件后本地完成。"""
        limit = max(1, min(100, int(self.config.get("message_limit") or 20)))
        return self._all_uids(imap)[-limit:]

    def _messages(self, mailbox: dict[str, Any], imap=None) -> list[tuple[str, str, datetime | None, str]]:
        own_connection = imap is None
        imap = imap or self._connect_imap()
        try:
            messages: list[tuple[str, str, datetime | None, str]] = []
            target = str(mailbox["address"]).strip().lower()
            for uid in reversed(self._candidate_uids(imap, target)):
                parsed = self._fetch_uid_message(imap, uid)
                if parsed is None:
                    continue
                message_id, text, received, headers = parsed
                if target not in headers.lower():
                    continue
                messages.append((message_id, text, received, headers))
            return messages
        finally:
            if own_connection:
                try:
                    imap.logout()
                except Exception:
                    pass

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        deadline = time.monotonic() + float(self.config.get("wait_timeout") or 360)
        reselect_interval = max(0.0, float(self.config.get("reselect_interval", 12)))
        reconnect_interval = max(0.0, float(self.config.get("reconnect_interval", 18)))
        imap = None
        connected_at = 0.0
        selected_at = 0.0
        try:
            while time.monotonic() < deadline:
                try:
                    now = time.monotonic()
                    if imap is not None and now - connected_at >= reconnect_interval:
                        self._close_imap(imap)
                        imap = None
                    if imap is None:
                        imap = self._connect_imap()
                        connected_at = now
                        selected_at = now

                    force_select = now - selected_at >= reselect_interval
                    self._refresh_selected_mailbox(imap, force_select=force_select)
                    if force_select:
                        selected_at = now

                    uidvalidity = self._uidvalidity(imap)
                    previous_uidvalidity = str(mailbox.get("_uidvalidity") or "")
                    if previous_uidvalidity and uidvalidity and previous_uidvalidity != uidvalidity:
                        mailbox["_seen_uids"] = set()
                        mailbox["_pending_uids"] = set()
                        mailbox["_last_uid"] = 0
                    if uidvalidity:
                        mailbox["_uidvalidity"] = uidvalidity

                    all_uids = self._all_uids(imap)
                    candidates = self._candidate_uid_window(all_uids, mailbox)
                    target = str(mailbox["address"]).strip().lower()
                    seen_uids = mailbox.setdefault("_seen_uids", set())
                    pending_uids = mailbox.setdefault("_pending_uids", set())
                    seen_message_ids = mailbox.setdefault("_seen_message_ids", mailbox.setdefault("_seen", set()))
                    seen_code_hashes = mailbox.setdefault("_seen_code_hashes", set())
                    used_code_hashes = mailbox.setdefault("_used_code_hashes", set())
                    clock_skew = max(0.0, float(self.config.get("clock_skew_seconds", 30)))
                    not_before = mailbox["_code_not_before"] - timedelta(seconds=clock_skew)
                    for uid in reversed(candidates):
                        parsed = self._fetch_uid_message(imap, uid)
                        if parsed is None:
                            pending_uids.add(uid)
                            continue
                        pending_uids.discard(uid)
                        seen_uids.add(uid)
                        message_id, text, received, headers = parsed
                        if target not in headers.lower():
                            continue
                        fingerprint = message_id or hashlib.sha256(text.encode()).hexdigest()
                        if fingerprint in seen_message_ids:
                            continue
                        seen_message_ids.add(fingerprint)
                        if received and received < not_before:
                            continue
                        match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", text, re.I) or re.search(r"(?<![#&])\b(\d{6})\b", text)
                        if match and match.group(1) != "177010":
                            code = match.group(1)
                            code_hash = hashlib.sha256(code.encode()).hexdigest()
                            if code_hash in used_code_hashes or code_hash in seen_code_hashes:
                                continue
                            seen_code_hashes.add(code_hash)
                            return code
                    if all_uids:
                        mailbox["_last_uid"] = max(int(uid) for uid in all_uids)
                except (imaplib.IMAP4.error, OSError):
                    self._close_imap(imap)
                    imap = None
                time.sleep(max(1, min(2, float(self.config.get("wait_interval") or 5))))
            return None
        finally:
            self._close_imap(imap)
