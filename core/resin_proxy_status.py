# -*- coding: utf-8 -*-
"""Resin 注册代理池状态与连通性检查。"""
from __future__ import annotations

import socket
import time
from urllib.parse import urlsplit


def _endpoint(proxy_url: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(str(proxy_url or ""))
        if not parsed.hostname or not parsed.port:
            return None
        return parsed.hostname, int(parsed.port)
    except (TypeError, ValueError):
        return None


def _safe_endpoint(proxy_url: str) -> str:
    try:
        parsed = urlsplit(str(proxy_url or ""))
        if not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{parsed.scheme or 'http'}://{host}:{parsed.port}" if parsed.port else f"{parsed.scheme or 'http'}://{host}"
    except (TypeError, ValueError):
        return ""


def _tcp_reachable(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def registration_proxy_status(*, check_tcp: bool = True) -> dict:
    from config import proxy as cfg
    from config import cloakbrowser as cloak_cfg
    from config import roxybrowser as registration_cfg

    pool = cfg.get_proxy_pool()
    required = bool(getattr(cfg, "REGISTRATION_PROXY_REQUIRED", False))
    configured_source = str(getattr(cfg, "REGISTRATION_PROXY_SOURCE", "") or "").strip().lower()
    resin_enabled = required or configured_source == "resin"
    mihomo_enabled = (not required) and (configured_source == "mihomo" or (not configured_source and bool(getattr(cfg, "MIHOMO_US_FALLBACK_ENABLED", False))))
    transparent = mihomo_enabled and bool(getattr(cfg, "MIHOMO_TRANSPARENT_ROUTING", False))
    route = str(getattr(cfg, "MIHOMO_REGISTRATION_ROUTE", "exclude") or "exclude").strip().lower()
    mihomo_mode = "mihomo_excluded" if route in {"exclude", "excluded", "country_filter"} else "mihomo_us"
    if mihomo_enabled:
        route_endpoint = (
            str(getattr(cfg, "MIHOMO_CONTROLLER_URL", "") or "").strip()
            if transparent
            else str(getattr(cfg, "MIHOMO_PROXY_URL", "") or "").strip()
        )
        pool = [route_endpoint]
        pool = [value for value in pool if value]
    cloak_proxy_enabled = bool(getattr(cloak_cfg, "CLOAK_USE_PROXY", True))
    driver = str(getattr(registration_cfg, "REGISTRATION_DRIVER", "") or "").strip().lower()
    endpoints: list[tuple[str, int]] = []
    endpoint_labels: list[str] = []
    for value in pool:
        endpoint = _endpoint(value)
        label = _safe_endpoint(value)
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
        if label and label not in endpoint_labels:
            endpoint_labels.append(label)

    online = bool(endpoints) if not check_tcp else False
    if check_tcp:
        online = any(_tcp_reachable(host, port) for host, port in endpoints[:3])

    error = ""
    if driver != "cloak":
        error = f"当前注册驱动为 {driver or '未配置'}，目标驱动应为 cloak"
    elif not cloak_proxy_enabled:
        error = "CloakBrowser 代理开关未开启"
    elif not pool:
        error = (
            "Mihomo Controller 地址为空"
            if transparent
            else "Mihomo 代理入口为空"
        ) if mihomo_enabled else "Resin 合格代理池为空"
    elif check_tcp and not online:
        error = (
            "Mihomo Controller 未连通"
            if transparent
            else "Mihomo 代理端口未连通"
        ) if mihomo_enabled else "Resin 代理端口未连通"

    ready = driver == "cloak" and cloak_proxy_enabled and bool(pool) and online
    loaded_from = getattr(cfg, "PROXY_POOL_LOADED_FROM", None)
    return {
        "required": required,
        "mode": f"{mihomo_mode}_transparent" if transparent else (mihomo_mode if mihomo_enabled else "resin"),
        "ready": ready,
        "driver": driver,
        "cloak_proxy_enabled": cloak_proxy_enabled,
        "qualified_count": len(pool),
        "endpoint_count": len(endpoints),
        "endpoint": endpoint_labels[0] if endpoint_labels else "",
        "online": online,
        "pool_source": f"{mihomo_mode}_controller" if transparent else (mihomo_mode if mihomo_enabled else ("verified_file" if loaded_from else "configured")),
        "preflight_required": bool(resin_enabled or mihomo_enabled),
        "pool_file": str(loaded_from or getattr(cfg, "PROXY_POOL_FILE", "") or ""),
        "management_url": str(getattr(cfg, "RESIN_MANAGEMENT_URL", "") or "").strip(),
        "error": error,
    }


def test_registration_proxy(*, target_url: str = "https://auth.openai.com/") -> dict:
    import requests
    from config import proxy as cfg

    selection = {}
    try:
        selection = cfg.pick_registration_proxy()
        selected = str(selection.get("proxy_url") or "")
    except Exception as exc:
        return {
            "ok": False,
            "error": f"注册代理选择/预检失败: {type(exc).__name__}",
            **registration_proxy_status(check_tcp=False),
        }
    started = time.monotonic()
    try:
        proxies = {"http": selected, "https": selected} if selected else None
        response = requests.get(
            target_url,
            proxies=proxies,
            timeout=(5, 12),
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 Resin registration connectivity check"},
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        ok = response.status_code not in {407, 502, 503, 504}
        response.close()
        return {
            "ok": ok,
            "http_status": int(response.status_code),
            "latency_ms": latency_ms,
            "target": "auth.openai.com",
            "endpoint": _safe_endpoint(selected),
            "mode": selection.get("mode") or "resin",
            "group": selection.get("group") or "",
            "node_name": selection.get("node_name") or "",
            "selection_ms": selection.get("selection_ms") or 0,
            "error": "" if ok else f"代理返回 HTTP {response.status_code}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "target": "auth.openai.com",
            "endpoint": _safe_endpoint(selected),
            "mode": selection.get("mode") or "resin",
            "group": selection.get("group") or "",
            "node_name": selection.get("node_name") or "",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
