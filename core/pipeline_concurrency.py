# -*- coding: utf-8 -*-
"""注册、测活、推送共用的并发闸门（双层：浏览器层 / 纯网络层）。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import wraps
from typing import Iterator


# 浏览器层：CloakBrowser 免费档只允许 1 个并发浏览器会话（151 内核），
# 注册、浏览器查活、Codex OAuth 共用这一个槽位，超了会被 license 拒绝。
PIPELINE_MAX_CONCURRENCY = 1

# 网络层：套餐查询 / 推送 / 提链只发 HTTPS 请求，不占浏览器会话。
# 与浏览器层分开计数，否则注册跑批期间"额度探测 / 查活快速路径"会被长期饿死。
PIPELINE_NET_CONCURRENCY = 2

# 只发网络请求的阶段；未列出的阶段一律按浏览器层限制（保守默认）。
_NET_STAGES = frozenset({"plan_check", "push", "extract_link"})

_LOCK = threading.Lock()


class _Gate:
    """一层闸门：信号量 + 观测计数。"""

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self._slots = threading.BoundedSemaphore(self.limit)
        self._active = 0
        self._peak = 0
        self._by_stage: dict[str, int] = {}

    def acquire(self, stage: str) -> None:
        self._slots.acquire()
        with _LOCK:
            self._active += 1
            self._peak = max(self._peak, self._active)
            self._by_stage[stage] = int(self._by_stage.get(stage, 0)) + 1

    def release(self, stage: str) -> None:
        with _LOCK:
            self._active -= 1
            remaining = int(self._by_stage.get(stage, 0)) - 1
            if remaining > 0:
                self._by_stage[stage] = remaining
            else:
                self._by_stage.pop(stage, None)
        self._slots.release()

    def snapshot(self) -> dict:
        with _LOCK:
            return {
                "limit": self.limit,
                "active": self._active,
                "peak": self._peak,
                "by_stage": dict(self._by_stage),
            }


_BROWSER_GATE = _Gate(PIPELINE_MAX_CONCURRENCY)
_NET_GATE = _Gate(PIPELINE_NET_CONCURRENCY)


def _gate_for(stage: str) -> _Gate:
    return _NET_GATE if stage in _NET_STAGES else _BROWSER_GATE


@contextmanager
def pipeline_slot(stage: str) -> Iterator[None]:
    """占用一个流水线槽位（浏览器层或网络层），重试期间也不释放。"""
    name = str(stage or "unknown")
    gate = _gate_for(name)
    gate.acquire(name)
    try:
        yield
    finally:
        gate.release(name)


def pipeline_snapshot() -> dict:
    browser = _BROWSER_GATE.snapshot()
    net = _NET_GATE.snapshot()
    by_stage = dict(browser["by_stage"])
    for key, value in net["by_stage"].items():
        by_stage[key] = int(by_stage.get(key, 0)) + int(value)
    return {
        "limit": PIPELINE_MAX_CONCURRENCY,
        "net_limit": PIPELINE_NET_CONCURRENCY,
        "active": browser["active"] + net["active"],
        "peak": max(browser["peak"], net["peak"]),
        "by_stage": by_stage,
        "browser": browser,
        "net": net,
    }


def pipeline_limited(stage: str):
    """给同步流水线入口加共享并发闸门。"""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with pipeline_slot(stage):
                return func(*args, **kwargs)

        return wrapped

    return decorator
