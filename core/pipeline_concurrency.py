# -*- coding: utf-8 -*-
"""注册、测活、推送共用的全局并发闸门。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import wraps
from typing import Iterator


# 硬上限：所有流水线阶段合计最多同时执行两个工作单元。
PIPELINE_MAX_CONCURRENCY = 2

_SLOTS = threading.BoundedSemaphore(PIPELINE_MAX_CONCURRENCY)
_LOCK = threading.Lock()
_ACTIVE = 0
_PEAK = 0
_BY_STAGE: dict[str, int] = {}


@contextmanager
def pipeline_slot(stage: str) -> Iterator[None]:
    """占用一个全局流水线槽位，重试期间也不释放。"""
    global _ACTIVE, _PEAK
    name = str(stage or "unknown")
    _SLOTS.acquire()
    with _LOCK:
        _ACTIVE += 1
        _PEAK = max(_PEAK, _ACTIVE)
        _BY_STAGE[name] = int(_BY_STAGE.get(name, 0)) + 1
    try:
        yield
    finally:
        with _LOCK:
            _ACTIVE -= 1
            remaining = int(_BY_STAGE.get(name, 0)) - 1
            if remaining > 0:
                _BY_STAGE[name] = remaining
            else:
                _BY_STAGE.pop(name, None)
        _SLOTS.release()


def pipeline_snapshot() -> dict:
    with _LOCK:
        return {
            "limit": PIPELINE_MAX_CONCURRENCY,
            "active": _ACTIVE,
            "peak": _PEAK,
            "by_stage": dict(_BY_STAGE),
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
