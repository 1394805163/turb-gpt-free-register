# -*- coding: utf-8 -*-
"""出口占用标记：mihomo transparent 模式下全局同一时刻只有一个出口。

注册任务开始时会切换并"占用"mihomo 组的出口节点，直到该任务结束；期间其他流程
（补密码 / 查活 / 套餐查询 / 补跑）一律不允许再切节点，否则正在跑的注册会在中途
换出口 —— 新号几分钟内跨国跳变是最典型的风控特征。

判定来源两条：
  1) 进程内计数 + 线程标记（同进程注册线程自己允许切）
  2) 跨进程 marker 文件 run/registration.active（pid/email/ts，注册结束即删除）

aux 流程拿到 registration_busy() 后不再切节点，直接用"当前选中的节点"出网。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_MARKER = Path(__file__).resolve().parent.parent / "run" / "registration.active"
_STALE_SECONDS = 900.0

_tls = threading.local()
_lock = threading.Lock()
_depth = 0


def _write_marker(email: str) -> None:
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(
            json.dumps({"pid": os.getpid(), "email": str(email or ""), "ts": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _clear_marker() -> None:
    try:
        _MARKER.unlink(missing_ok=True)
    except Exception:
        pass


def begin_registration(email: str = "") -> None:
    """注册流程开始：占用全局出口。"""
    global _depth
    with _lock:
        _depth += 1
    _tls.registration = True
    _write_marker(email)


def end_registration() -> None:
    global _depth
    with _lock:
        _depth = max(0, _depth - 1)
        empty = _depth == 0
    _tls.registration = False
    if empty:
        _clear_marker()


def in_registration_flow() -> bool:
    """当前线程是否属于注册流程（注册自己允许切节点）。"""
    return bool(getattr(_tls, "registration", False))


def _foreign_marker_fresh() -> bool:
    try:
        if not _MARKER.exists():
            return False
        data = json.loads(_MARKER.read_text(encoding="utf-8") or "{}")
        pid = int(data.get("pid") or 0)
        age = time.time() - float(data.get("ts") or _MARKER.stat().st_mtime)
        if age > _STALE_SECONDS:
            return False
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)  # 进程还活着才算占用
            except OSError:
                return False
        return True
    except Exception:
        return False


def registration_busy() -> bool:
    """是否有注册正在占用出口（含其他进程）。"""
    with _lock:
        if _depth > 0:
            return True
    return _foreign_marker_fresh()


def registration_guard(func):
    """给注册入口加"占用出口"语义（进入占用，退出释放）。"""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        email = str(kwargs.get("email") or (args[0] if args else "") or "")
        begin_registration(email)
        try:
            return func(*args, **kwargs)
        finally:
            end_registration()

    return wrapper


if __name__ == "__main__":
    assert registration_busy() is False, "初始不该占用"
    begin_registration("demo@example.com")
    assert in_registration_flow() is True
    assert registration_busy() is True
    assert _MARKER.exists(), "应写入跨进程 marker"
    end_registration()
    assert in_registration_flow() is False and registration_busy() is False
    assert not _MARKER.exists(), "释放后应删除 marker"
    guarded = registration_guard(lambda *a, **k: (registration_busy(), in_registration_flow()))
    assert guarded("x@y.com") == (True, True)
    assert registration_busy() is False
    print("route_lock self-check OK")
