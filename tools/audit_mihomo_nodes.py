# -*- coding: utf-8 -*-
"""Mihomo 节点体检：逐个实测「节点名 → 真实出口国家/IP」，找出名实不符的节点。

用法：
    python tools/audit_mihomo_nodes.py            # 只测名称含国家关键词的节点（快）
    python tools/audit_mihomo_nodes.py --all      # 全量叶子节点
    python tools/audit_mihomo_nodes.py --filter JP
注意：会逐个切换 Mihomo 节点（影响走该组的流量），请在空闲时运行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

_COUNTRY_LABELS = (
    ("JP", ("JP", "JAPAN", "日本")),
    ("SG", ("SG", "SINGAPORE", "新加坡")),
    ("US", ("US", "UNITED", "美国")),
    ("TW", ("TW", "TAIWAN", "台湾")),
    ("HK", ("HK", "HONGKONG", "香港")),
)
_KEYWORDS = tuple(k for _, labels in _COUNTRY_LABELS for k in labels)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="", help="只测节点名包含该关键词的")
    ap.add_argument("--all", action="store_true", help="测全部节点（默认只测含国家关键词的）")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    import requests
    from config import proxy as cfg

    base = str(cfg.MIHOMO_CONTROLLER_URL or "").rstrip("/")
    secret = str(cfg.MIHOMO_CONTROLLER_SECRET or "")
    group = str(cfg.MIHOMO_REGISTRATION_GROUP or cfg.MIHOMO_US_GROUP or "").strip()
    proxy_url = str(cfg.MIHOMO_PROXY_URL or "").strip()
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    if not (base and group):
        print("Mihomo 配置不完整（controller/group 缺失）")
        return 2

    resp = requests.get(f"{base}/proxies/{group}", headers=headers, timeout=10)
    resp.raise_for_status()
    names = [str(n) for n in ((resp.json() or {}).get("all") or [])]

    if args.filter:
        needle = args.filter.upper()
        names = [n for n in names if needle in n.upper()]
    elif not args.all:
        names = [n for n in names if any(k.upper() in n.upper() for k in _KEYWORDS)]

    # 透明路由模式：流量经系统路由被 Mihomo 接管，本机 MIHOMO_PROXY_URL 可能并不监听 → 直连测。
    use_proxies = None
    if proxy_url:
        try:
            import socket as _socket
            from urllib.parse import urlparse as _urlparse
            _u = _urlparse(proxy_url)
            _s = _socket.create_connection((_u.hostname or "127.0.0.1", int(_u.port or 7897)), timeout=1.2)
            _s.close()
            use_proxies = {"http": proxy_url, "https": proxy_url}
            print(f"代理通道: {proxy_url}")
        except Exception:
            print("未检测到本地代理端口（透明路由模式）→ 使用直连测出口")
    else:
        print("未配置 MIHOMO_PROXY_URL → 使用直连测出口")

    print(f"待测节点: {len(names)}（组: {group}）")
    results = []
    for idx, name in enumerate(names, 1):
        try:
            put = requests.put(
                f"{base}/proxies/{group}",
                headers={**headers, "Content-Type": "application/json"},
                json={"name": name},
                timeout=10,
            )
            put.raise_for_status()
            time.sleep(0.4)
            tr = requests.get(
                "https://auth.openai.com/cdn-cgi/trace",
                proxies=use_proxies,
                timeout=(3.0, args.timeout),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
            )
            country = ip = ""
            for line in (tr.text or "").splitlines():
                if line.startswith("loc="):
                    country = line[4:].strip().upper()
                elif line.startswith("ip="):
                    ip = line[4:].strip()
            results.append({"node": name, "exit_country": country, "exit_ip": ip})
            print(f"[{idx}/{len(names)}] {name} -> {country or '?'} ({ip or '-'})")
        except Exception as exc:
            results.append({"node": name, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            print(f"[{idx}/{len(names)}] {name} -> ERR {type(exc).__name__}")

    out = Path("run/logs") / f"mihomo-node-audit-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n报告已保存: {out}")

    mismatch = []
    for r in results:
        if r.get("error") or not r.get("exit_country"):
            continue
        nm = r["node"].upper()
        for code, labels in _COUNTRY_LABELS:
            if any(x in nm for x in labels) and r["exit_country"] != code:
                mismatch.append(r)
                break
    if mismatch:
        print("\n名实不符节点（节点名暗示的国家 != 实际出口）:")
        for r in mismatch:
            print(f"  {r['node']} -> 实际 {r['exit_country']} ({r['exit_ip']})")
    else:
        print("\n没有发现名实不符节点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
