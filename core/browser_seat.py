# -*- coding: utf-8 -*-
"""跨进程 CloakBrowser 席位锁。

CloakBrowser 免费档只允许 1 个并发会话。WebUI（查活 / 注册流水线）和外部工具
（收口脚本、探针）如果同时开浏览器，双方都会撞上 `session limit reached`，
彼此白跑一轮。

这里用**操作系统文件锁**做跨进程互斥：
  - 正常退出：锁立即释放
  - 进程被 kill：操作系统关闭句柄 → 锁同样立即释放
    （比服务端约 15 分钟的租约快得多，也不会再出现"席位被占死"）
"""
from __future__ import annotations

import atexit
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - 生产环境是 Windows
    import fcntl

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "cloakbrowser-seat.lock")
_LOCK = threading.RLock()
_HOLDER: list = [None, 0]  # [fd, depth]


def _lock_fd(fd: int) -> bool:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire(*, timeout: float = 1800.0, poll: float = 1.5, label: str = "") -> bool:
    """获取浏览器席位；同进程内可重入，跨进程互斥。"""
    deadline = time.monotonic() + max(1.0, float(timeout))
    start = time.monotonic()
    warned = False
    while True:
        with _LOCK:
            if _HOLDER[0] is not None:
                _HOLDER[1] += 1
                return True
            fd = os.open(_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
            if _lock_fd(fd):
                _HOLDER[0] = fd
                _HOLDER[1] = 1
                if warned:
                    logger.info("[席位] 已获得浏览器席位（等待 %.0fs）%s", time.monotonic() - start, label)
                return True
            os.close(fd)
        if not warned:
            warned = True
            logger.info("[席位] 浏览器席位居满，等待释放中…%s", label)
        if time.monotonic() >= deadline:
            logger.warning("[席位] 等待浏览器席位超时（%.0fs）%s", timeout, label)
            return False
        time.sleep(poll)


def release() -> None:
    """释放浏览器席位（重入计数归零才真正解锁）。"""
    with _LOCK:
        if _HOLDER[0] is None:
            return
        _HOLDER[1] -= 1
        if _HOLDER[1] > 0:
            return
        fd = _HOLDER[0]
        _HOLDER[0] = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


atexit.register(release)
