# -*- coding: utf-8 -*-
"""注册浏览器与 OAuth 浏览器之间的进程内互斥闸门。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


_SLOT = threading.BoundedSemaphore(1)


@contextmanager
def browser_task_slot(stage: str, *, timeout: float | None = None) -> Iterator[bool]:
    """最多允许一个注册/OAuth浏览器阶段运行；超时只返回 False。"""
    if timeout is None:
        acquired = _SLOT.acquire()
    else:
        acquired = _SLOT.acquire(timeout=max(0.0, float(timeout)))
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        _SLOT.release()
