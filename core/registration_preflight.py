# -*- coding: utf-8 -*-
"""轻量注册代理预检。

预检只验证代理的网络出口与 OpenAI 同域 trace，不启动浏览器，也不把
ChatGPT 的 403 challenge 直接判为失败；浏览器会负责执行 JS challenge。
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_REGISTRATION_COMMON_COUNTRIES = {"US", "JP", "SG", "KR", "TW"}

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 180.0


def _safe_endpoint(proxy: str | None) -> str:
    try:
        p = urlsplit(str(proxy or ""))
        if not p.hostname:
            return ""
        host = p.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{p.scheme or 'http'}://{host}:{p.port}" if p.port else f"{p.scheme or 'http'}://{host}"
    except (TypeError, ValueError):
        return ""


def _trace_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip().lower()] = value.strip()
    return fields


def preflight_proxy(
    proxy: str | None,
    *,
    require_country: str = "US",
    allowed_countries: object = None,
    excluded_countries: object = None,
    allow_transparent: bool = False,
    route_identity: str | None = None,
    force: bool = False,
) -> dict:
    """返回代理预检结果；结果带短缓存，避免同一节点重复打探。

    `chatgpt_status=403` 仅作为信息记录，因为该接口会返回需要浏览器 JS
    执行的 Cloudflare challenge，不能用 requests 的 403 否定浏览器可用性。
    """
    value = str(proxy or "").strip()
    def _countries(raw: object) -> set[str]:
        if isinstance(raw, str):
            raw = raw.replace(",", " ").replace(";", " ").split()
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return set()
        return {str(item).strip().upper() for item in raw if str(item).strip()}

    allowed = _countries(allowed_countries)
    excluded = _countries(excluded_countries)
    if not value and not allow_transparent:
        return {"ok": False, "reason": "代理地址为空", "endpoint": ""}
    cache_key = "|".join([
        value,
        str(route_identity or "").strip(),
        ",".join(sorted(allowed)),
        ",".join(sorted(excluded)),
        "transparent" if allow_transparent else "explicit",
        str(require_country or "").strip().upper(),
    ])
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and now - cached[0] < _CACHE_TTL:
            return {**cached[1], "cached": True}

    result: dict = {"ok": False, "endpoint": _safe_endpoint(value), "country": "", "ip": "", "latency_ms": 0}
    started = time.monotonic()
    try:
        import requests

        proxies = {"http": value, "https": value} if value else None
        response = requests.get(
            "https://auth.openai.com/cdn-cgi/trace",
            proxies=proxies,
            timeout=(3.0, 8.0),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
        )
        fields = _trace_fields(response.text)
        result.update(
            status=int(response.status_code),
            country=str(fields.get("loc") or "").upper(),
            ip=str(fields.get("ip") or ""),
            colo=str(fields.get("colo") or ""),
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        wanted = str(require_country or "").strip().upper()
        if response.status_code != 200:
            result["reason"] = f"OpenAI trace HTTP {response.status_code}"
        elif wanted and result["country"] != wanted:
            result["reason"] = f"出口国家为 {result['country'] or '未知'}，需要 {wanted}"
        elif allowed:
            country = result["country"]
            explicit_allowed = allowed - {"OTHER"}
            allowed_as_other = "OTHER" in allowed and bool(country) and country not in _REGISTRATION_COMMON_COUNTRIES
            if country not in explicit_allowed and not allowed_as_other:
                result["reason"] = f"出口国家为 {country or '未知'}，不在允许地区内"
            elif country in excluded:
                result["reason"] = f"出口国家为 {country or '未知'}，属于排除地区"
            else:
                result["ok"] = True
                result["reason"] = "网络出口预检通过"
        elif result["country"] in excluded:
            result["reason"] = f"出口国家为 {result['country'] or '未知'}，属于排除地区"
        else:
            result["ok"] = True
            result["reason"] = "网络出口预检通过"
    except Exception as exc:
        result.update(
            latency_ms=round((time.monotonic() - started) * 1000),
            reason=f"{type(exc).__name__}: {str(exc)[:180]}",
        )

    with _LOCK:
        _CACHE[cache_key] = (time.monotonic(), dict(result))
    return result
