# -*- coding: utf-8 -*-
"""WebUI 看门狗：端口不通就自动拉起（后台常驻，无外部依赖）。

用法：python tools/webui_watchdog.py [--interval 60]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)


def _alive(url: str = "http://127.0.0.1:5000/login") -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()
    log = os.path.join(WT, "run", "webui_watchdog.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)

    def note(text: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {text}"
        print(line, flush=True)
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    note("watchdog 启动")
    while True:
        time.sleep(max(10.0, float(args.interval)))
        if _alive():
            continue
        note("WebUI 无响应，执行 manage.ps1 -Action start")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 os.path.join(WT, "manage.ps1"), "-Action", "start"],
                cwd=WT, capture_output=True, timeout=180,
            )
        except Exception as exc:
            note(f"拉起失败：{type(exc).__name__}: {exc}")
            continue
        note("已拉起" if _alive() else "拉起后仍无响应")


if __name__ == "__main__":
    sys.exit(main())
