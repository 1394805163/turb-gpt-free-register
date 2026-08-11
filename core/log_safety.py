# -*- coding: utf-8 -*-
"""日志中的稳定隐私标识。"""
from __future__ import annotations

import hashlib
import re


_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)


def _normalized_email(email: str) -> str:
    return str(email or "").strip().lower()


def email_fingerprint(email: str) -> str:
    """返回可关联同一邮箱、但不能还原地址的短标识。"""
    normalized = _normalized_email(email)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]


def redact_email(email: str) -> str:
    """隐藏邮箱本地部分和域名主体，并保留稳定短哈希用于排障。"""
    normalized = _normalized_email(email)
    fingerprint = email_fingerprint(normalized)
    local, separator, domain = normalized.rpartition("@")
    if not separator or not local or not domain:
        return f"<email#{fingerprint}>"

    local_hint = f"{local[0]}***{local[-1]}" if len(local) > 1 else f"{local[0]}***"
    domain_head, dot, domain_tail = domain.partition(".")
    domain_hint = f"{domain_head[0]}***" if domain_head else "***"
    if dot and domain_tail:
        domain_hint = f"{domain_hint}.{domain_tail}"
    return f"{local_hint}@{domain_hint}#{fingerprint}"


def redact_emails(value: object) -> str:
    """脱敏异常、路径等自由文本中出现的全部邮箱地址。"""
    return _EMAIL_RE.sub(lambda match: redact_email(match.group(0)), str(value or ""))
