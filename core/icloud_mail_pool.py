from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
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
        payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        previous = None
        try:
            previous = self.state_file.stat()
        except FileNotFoundError:
            pass
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_file.parent,
                prefix=f".{self.state_file.name}.",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, stat.S_IMODE(previous.st_mode) if previous else 0o600)
            # Windows 没有 geteuid/chown；权限和属主继承只在 POSIX 且 API
            # 可用时执行，不能让邮箱状态持久化因平台差异失败。
            geteuid = getattr(os, "geteuid", None)
            chown = getattr(os, "chown", None)
            if previous and callable(geteuid) and callable(chown) and geteuid() == 0:
                chown(temp_name, previous.st_uid, previous.st_gid)
            os.replace(temp_name, self.state_file)
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

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
            candidates = [item for item in addresses if self._available(state.get(item))]
            if not candidates:
                raise RuntimeError(f"iCloud 邮箱池暂无可用邮箱（共 {len(addresses)} 个）")
            # Hide My Email aliases are not quality-ordered. Random selection prevents
            # a run of historical/deactivated aliases at the head of an imported file
            # from starving the registration pipeline.
            address = secrets.choice(candidates)
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

    def _request_timeout(self, *, deadline: float | None = None, fallback: float | None = None) -> float:
        configured = float(fallback if fallback is not None else (self.config.get("request_timeout") or 30))
        timeout = max(0.01, configured)
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("iCloud OTP 等待总预算已耗尽")
        return min(timeout, remaining)

    def _bind_deadline(self, imap, deadline: float | None, *, fallback: float | None = None) -> float:
        timeout = self._request_timeout(deadline=deadline, fallback=fallback)
        self._bound_socket_timeout(imap, timeout)
        return timeout

    def _connect_imap(
        self,
        request_timeout: float | None = None,
        *,
        deadline: float | None = None,
    ):
        username = str(self.config.get("imap_username") or "").strip()
        password = str(self.config.get("imap_password") or "").strip()
        if not username or not password:
            raise RuntimeError("请配置 iCloud IMAP 邮箱和 Apple 专用密码")
        timeout = self._request_timeout(deadline=deadline, fallback=request_timeout)
        imap = imaplib.IMAP4_SSL(
            str(self.config.get("imap_host") or "imap.mail.me.com"),
            int(self.config.get("imap_port") or 993),
            timeout=max(0.000001, timeout),
        )
        try:
            self._bind_deadline(imap, deadline, fallback=request_timeout)
            imap.login(username, password)
            self._bind_deadline(imap, deadline, fallback=request_timeout)
            status, _ = imap.select(str(self.config.get("imap_mailbox") or "INBOX"), readonly=True)
        except Exception:
            self._close_imap(imap, deadline=deadline)
            raise
        if status != "OK":
            self._close_imap(imap, deadline=deadline)
            raise RuntimeError("iCloud IMAP 打开收件箱失败")
        return imap

    def _mailbox_name(self) -> str:
        return str(self.config.get("imap_mailbox") or "INBOX")

    @staticmethod
    def _abort_imap(imap) -> None:
        if imap is None:
            return
        try:
            shutdown = getattr(imap, "shutdown", None)
            if callable(shutdown):
                shutdown()
                return
        except Exception:
            pass
        try:
            sock = getattr(imap, "sock", None)
            if sock is not None:
                sock.close()
        except Exception:
            pass

    def _close_imap(self, imap, *, deadline: float | None = None) -> None:
        if imap is None:
            return
        if deadline is not None:
            try:
                self._bind_deadline(imap, deadline)
            except TimeoutError:
                self._abort_imap(imap)
                return
        try:
            imap.logout()
        except Exception:
            self._abort_imap(imap)

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

    def _refresh_selected_mailbox(
        self,
        imap,
        *,
        force_select: bool = False,
        deadline: float | None = None,
    ) -> None:
        self._bind_deadline(imap, deadline)
        status, _ = imap.noop()
        if status != "OK":
            raise imaplib.IMAP4.error("iCloud IMAP NOOP 失败")
        if force_select:
            self._bind_deadline(imap, deadline)
            status, _ = imap.select(self._mailbox_name(), readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error("iCloud IMAP 重新选择收件箱失败")

    @staticmethod
    def _bound_socket_timeout(imap, timeout: float) -> None:
        try:
            sock = getattr(imap, "sock", None)
            if sock is not None:
                sock.settimeout(max(0.000001, float(timeout)))
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    def _all_uids(self, imap, *, deadline: float | None = None) -> list[bytes]:
        self._bind_deadline(imap, deadline)
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

    def _parse_uid_message(self, uid: bytes, raw: bytes) -> tuple[str, str, datetime | None, str, set[str]] | None:
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
        recipient_values: list[str] = []
        for header_name in (
            "To", "Cc", "Bcc", "Delivered-To", "X-Original-To", "X-Apple-Original-To",
            "Envelope-To", "X-Envelope-To", "Apparently-To", "Resent-To",
        ):
            recipient_values.extend(self._decode(value) for value in message.get_all(header_name, []))
        recipients = {
            str(address or "").strip().lower()
            for _display_name, address in getaddresses(recipient_values)
            if "@" in str(address or "")
        }
        message_id = str(message.get("Message-ID") or uid.decode(errors="replace"))
        return message_id, text, received, headers, recipients

    def _fetch_uid_message(
        self,
        imap,
        uid: bytes,
        *,
        deadline: float | None = None,
    ) -> tuple[str, str, datetime | None, str, set[str]] | None:
        self._bind_deadline(imap, deadline)
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
                message_id, text, received, headers, recipients = parsed
                if target not in recipients:
                    continue
                messages.append((message_id, text, received, headers))
            return messages
        finally:
            if own_connection:
                self._close_imap(imap)

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
                    remaining_budget = deadline - now
                    if remaining_budget <= 0:
                        break
                    request_timeout = min(
                        max(0.01, float(self.config.get("request_timeout") or 30)),
                        remaining_budget,
                    )
                    if imap is not None and now - connected_at >= reconnect_interval:
                        self._close_imap(imap, deadline=deadline)
                        imap = None
                    if imap is None:
                        imap = self._connect_imap(
                            request_timeout=request_timeout,
                            deadline=deadline,
                        )
                        connected_at = now
                        selected_at = now

                    force_select = now - selected_at >= reselect_interval
                    self._refresh_selected_mailbox(
                        imap,
                        force_select=force_select,
                        deadline=deadline,
                    )
                    if force_select:
                        selected_at = now

                    uidvalidity = self._uidvalidity(imap)
                    previous_uidvalidity = str(mailbox.get("_uidvalidity") or "")
                    if previous_uidvalidity and uidvalidity and previous_uidvalidity != uidvalidity:
                        mailbox.setdefault("_seen_uids", set()).clear()
                        mailbox.setdefault("_pending_uids", set()).clear()
                        mailbox["_last_uid"] = 0
                    if uidvalidity:
                        mailbox["_uidvalidity"] = uidvalidity

                    all_uids = self._all_uids(imap, deadline=deadline)
                    candidates = self._candidate_uid_window(all_uids, mailbox)
                    if all_uids:
                        # 在可能提前返回验证码前推进基线；临时 FETCH 失败由 pending_uids 保证重试。
                        mailbox["_last_uid"] = max(int(uid) for uid in all_uids)
                    target = str(mailbox["address"]).strip().lower()
                    seen_uids = mailbox.setdefault("_seen_uids", set())
                    pending_uids = mailbox.setdefault("_pending_uids", set())
                    seen_message_ids = mailbox.setdefault("_seen_message_ids", mailbox.setdefault("_seen", set()))
                    seen_code_hashes = mailbox.setdefault("_seen_code_hashes", set())
                    used_code_hashes = mailbox.setdefault("_used_code_hashes", set())
                    clock_skew = max(0.0, float(self.config.get("clock_skew_seconds", 30)))
                    not_before = mailbox["_code_not_before"] - timedelta(seconds=clock_skew)
                    for uid in reversed(candidates):
                        parsed = self._fetch_uid_message(imap, uid, deadline=deadline)
                        if parsed is None:
                            pending_uids.add(uid)
                            continue
                        pending_uids.discard(uid)
                        seen_uids.add(uid)
                        message_id, text, received, _headers, recipients = parsed
                        if target not in recipients:
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
                except (imaplib.IMAP4.error, OSError):
                    self._close_imap(imap, deadline=deadline)
                    imap = None
                remaining_budget = deadline - time.monotonic()
                if remaining_budget <= 0:
                    break
                poll_delay = max(1, min(2, float(self.config.get("wait_interval") or 5)))
                time.sleep(min(poll_delay, remaining_budget))
            return None
        finally:
            self._close_imap(imap, deadline=deadline)
