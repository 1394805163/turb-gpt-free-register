# -*- coding: utf-8 -*-
"""
注册任务服务层：
    - 线程池并发执行 run_registration
    - 每个任务在 data/registration_jobs.json 里有一条记录
    - 每个任务的日志写到 data/logs/<job_uuid>.log，便于 Web UI 实时尾巴

使用：
    submit_registration(email_source="outlook", count=5)
    → 创建 5 个任务，丢入线程池，立即返回 [job_dict, ...]
"""
import logging
import multiprocessing as mp
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from queue import Empty
from typing import Any

from core import codex_retry_service, db
from core.log_safety import redact_email
from core.pipeline_concurrency import pipeline_slot

logger = logging.getLogger(__name__)

# 全局线程池，最大并发数（WebUI 每次提交时可按最新 workers 重建）
_DEFAULT_MAX_WORKERS = 1
_MIN_MAX_WORKERS = 1
_MAX_MAX_WORKERS = 2
_executor: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_MAX_WORKERS
_executor_generation = 0
_retired_executors: list[ThreadPoolExecutor] = []
_executor_lock = threading.RLock()

_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_THREAD_CTX = threading.local()
_ACTIVE_BROWSER_DRIVERS: dict[int, object] = {}
_ACTIVE_REGISTRATION_PROCESSES: dict[int, mp.Process] = {}
_ACTIVE_BROWSER_LOCK = threading.RLock()

# 一台 1.6 GiB 内存主机上，一个 CloakBrowser 通常会占用数百 MiB；两个浏览器
# 同时启动会迅速把页面渲染、Gunicorn 请求线程和 Swap 一起拖慢。线程池仍可保留
# 排队能力，但实际浏览器阶段只允许一个任务占用资源槽，避免 WebUI 被浏览器抢空。
_REGISTRATION_BROWSER_SLOTS = threading.BoundedSemaphore(1)
_JOB_TIMEOUT_DEFAULT_SECONDS = 15 * 60


class StopRequested(RuntimeError):
    """用户手动停止注册任务。"""


class RegistrationJobTimeout(TimeoutError):
    """注册子进程超过总时限，已被父任务终止。"""


def register_active_browser(job_id: int, driver: object) -> None:
    """登记任务当前浏览器，供停止/总时长看门狗主动释放阻塞的 RPC。"""
    if not driver:
        return
    with _ACTIVE_BROWSER_LOCK:
        _ACTIVE_BROWSER_DRIVERS[int(job_id)] = driver


def unregister_active_browser(job_id: int, driver: object | None = None) -> None:
    """移除任务浏览器登记；driver 参数用于避免旧实例误删新实例。"""
    with _ACTIVE_BROWSER_LOCK:
        current = _ACTIVE_BROWSER_DRIVERS.get(int(job_id))
        if driver is None or current is driver:
            _ACTIVE_BROWSER_DRIVERS.pop(int(job_id), None)


def _register_active_registration_process(job_id: int, process: mp.Process) -> None:
    with _ACTIVE_BROWSER_LOCK:
        _ACTIVE_REGISTRATION_PROCESSES[int(job_id)] = process


def _unregister_active_registration_process(job_id: int, process: mp.Process | None = None) -> None:
    with _ACTIVE_BROWSER_LOCK:
        current = _ACTIVE_REGISTRATION_PROCESSES.get(int(job_id))
        if process is None or current is process:
            _ACTIVE_REGISTRATION_PROCESSES.pop(int(job_id), None)


def _terminate_registration_process(process: mp.Process, reason: str) -> None:
    """Terminate the isolated registration process and its browser descendants."""
    # ``Process.close()`` can race with the watchdog timer: the task may have
    # finished while the timer is already dispatching cleanup.  Accessing
    # ``pid``/``is_alive`` on a closed multiprocessing object raises
    # ``ValueError``; cleanup must remain idempotent in that case.
    try:
        pid = process.pid
    except (AttributeError, ValueError):
        logger.info("[Service] 注册子进程已由任务收尾（%s），跳过重复清理", reason)
        return
    if not pid:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = None
    try:
        if pgid and pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.warning("[Service] 终止注册子进程失败（%s）：%s", reason, exc)
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.join(timeout=5)
    except (AttributeError, ValueError):
        logger.info("[Service] 注册子进程清理期间已关闭（%s，pid=%s）", reason, pid)
        return
    try:
        alive = process.is_alive()
    except (AttributeError, ValueError):
        alive = False
    if alive:
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.join(timeout=3)
        except (AttributeError, ValueError):
            pass
    logger.warning("[Service] 注册子进程已终止（%s，pid=%s）", reason, pid)


def _force_close_active_browser(job_id: int, reason: str) -> None:
    """停止任务时优先终止隔离子进程，旧浏览器登记仅作兼容兜底。"""
    with _ACTIVE_BROWSER_LOCK:
        process = _ACTIVE_REGISTRATION_PROCESSES.get(int(job_id))
    if process is not None:
        threading.Thread(
            target=_terminate_registration_process,
            args=(process, reason),
            name=f"reg-process-stop-{job_id}",
            daemon=True,
        ).start()
        _append_job_log(job_id, f"看门狗已终止注册子进程（{reason}）。")
        return
    with _ACTIVE_BROWSER_LOCK:
        driver = _ACTIVE_BROWSER_DRIVERS.get(int(job_id))
    if not driver:
        _append_job_log(job_id, f"看门狗未找到活动浏览器实例（{reason}），继续结束任务。")
        logger.warning("[Service] 任务 #%s %s，但没有登记中的浏览器实例", job_id, reason)
        return

    def _close() -> None:
        try:
            driver.quit()
            _append_job_log(job_id, f"看门狗已请求关闭当前浏览器（{reason}）。")
            logger.warning("[Service] 已关闭任务 #%s 的浏览器实例（%s）", job_id, reason)
        except Exception as exc:
            _append_job_log(job_id, f"看门狗关闭浏览器调用异常（{reason}）：{type(exc).__name__}。")
            logger.warning("[Service] 关闭任务 #%s 浏览器失败（%s）：%s", job_id, reason, exc)

    threading.Thread(target=_close, name=f"reg-browser-stop-{job_id}", daemon=True).start()


def _acquire_registration_browser_slot(job_id: int) -> bool:
    """等待浏览器资源槽；排队期间可响应停止/取消，不会永久卡住线程。"""
    while True:
        if is_stop_requested(job_id):
            return False
        if _REGISTRATION_BROWSER_SLOTS.acquire(timeout=1.0):
            return True


@contextmanager
def _registration_browser_slot(job_id: int):
    """限制 CloakBrowser 实例数量；等待槽位时不占用全局流水线槽。"""
    acquired = _acquire_registration_browser_slot(job_id)
    if not acquired:
        raise StopRequested(f"任务 #{job_id} 在等待浏览器资源槽时被停止")
    try:
        yield
    finally:
        _REGISTRATION_BROWSER_SLOTS.release()


def _job_timeout_seconds() -> int:
    try:
        value = int(os.getenv("REGISTRATION_JOB_TIMEOUT_SECONDS", str(_JOB_TIMEOUT_DEFAULT_SECONDS)))
    except (TypeError, ValueError):
        value = _JOB_TIMEOUT_DEFAULT_SECONDS
    return max(180, min(3600, value))


def _start_job_watchdog(job_id: int) -> threading.Timer:
    """给单个注册任务设置总时长上限，避免代理轮换/页面等待无限占用线程。"""
    timeout = _job_timeout_seconds()

    def _expire() -> None:
        job = db.get_job(job_id)
        if not job or job.get("status") not in ("running", "stopping"):
            return
        with _STOP_LOCK:
            event = _STOP_EVENTS.get(int(job_id))
            if event is not None:
                event.set()
        db.update_job(
            job_id,
            status="stopping",
            error=f"自动超时：单任务已运行超过 {timeout} 秒，正在清理浏览器",
        )
        _append_job_log(job_id, f"自动看门狗：超过 {timeout} 秒，已发送停止信号并清理当前浏览器。")
        _force_close_active_browser(job_id, f"自动超时 {timeout}s")
        logger.warning("[Service] 注册任务 #%s 达到总时长上限 %ss", job_id, timeout)

    timer = threading.Timer(timeout, _expire)
    timer.daemon = True
    timer.start()
    return timer


def _activate_job(job_id: int) -> None:
    _THREAD_CTX.job_id = int(job_id)
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))


def _deactivate_job(job_id: int) -> None:
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))
    try:
        delattr(_THREAD_CTX, "job_id")
    except Exception:
        pass


def is_stop_requested(job_id: int | None = None) -> bool:
    if job_id is None:
        job_id = getattr(_THREAD_CTX, "job_id", None)
    if not job_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_EVENTS.get(int(job_id))
        if ev and ev.is_set():
            return True
    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop_requested() -> None:
    job_id = getattr(_THREAD_CTX, "job_id", None)
    if is_stop_requested(job_id):
        raise StopRequested(f"任务 #{job_id} 已被用户手动停止")


def _append_job_log(job_id: int, message: str) -> None:
    try:
        job = db.get_job(job_id)
        log_file = job.get("log_file") if job else None
        if not log_file:
            return
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with Path(log_file).open("a", encoding="utf-8") as f:
            f.write(f"{ts} [WARNING] [job-control] {message}\n")
    except Exception:
        pass


def _random_display_name() -> str:
    """生成符合 OpenAI 限制的英文字母显示名。"""
    from core.name_samples import random_display_name

    return random_display_name()


def _prepare_registration_args() -> tuple[str, str, str]:
    """复用 CLI 的默认规则，为旧 Web 任务入口补齐注册参数。"""
    # 用模块属性读，支持 WebUI 热加载
    from config import register as _r, email as _e
    from core.email_provider import acquire_email
    from core.profile_utils import generate_random_birthday

    email = str(getattr(_r, "REGISTER_EMAIL", "") or "").strip()
    name = str(getattr(_r, "REGISTER_NAME", "") or "").strip()
    # WebUI/配置里有时会把空值存成 "-"，这不是合法 OpenAI 显示名，按空处理并自动生成
    if name in {"-", "—", "无", "空", "none", "None", "null", "NULL"}:
        name = ""

    if not name:
        # 手动模式也自动生成显示名，减少配置负担
        name = _random_display_name()

    birthday = generate_random_birthday()

    # 邮箱领取会把池状态置为 used，因此放在所有其他准备逻辑之后。
    if not email:
        if _e.USE_EMAIL_SERVICE:
            email = acquire_email()
        else:
            raise RuntimeError(
                "手动模式未配置邮箱。请在 WebUI 配置页设置 REGISTER_EMAIL，"
                "或开启 USE_EMAIL_SERVICE 并从邮箱池领取。"
            )

    return email, name, birthday


def _release_unconsumed_job_email(email: str | None, reason: str) -> None:
    """任务失败兜底：只回收尚未生成账号、仍处于 used 的邮箱领取。"""
    if not email:
        return
    try:
        from core.email_provider import release_email_if_unconsumed

        release_email_if_unconsumed(email, note=f"任务未消耗，已自动回收: {reason[:180]}")
    except Exception:
        logger.exception("[Service] 回收未消耗邮箱失败: %s", redact_email(email))


def _is_final_session_access_token_timeout(error: object) -> bool:
    """
    识别注册最后一步已经返回 /api/auth/session 200 但没有 accessToken 的失败。
    这种邮箱后续继续注册通常会卡在同一状态，按要求直接停用邮箱池条目。
    """
    text = str(error or "")
    if not text:
        return False
    return (
        "等待 /api/auth/session accessToken 超时" in text
        and "WARNING_BANNER" in text
        and "'_http_status': 200" in text
    )


def _should_disable_failed_registration_email(error: object) -> bool:
    """需要直接停用邮箱的注册失败类型。"""
    text = str(error or "")
    if not text:
        return False
    # 只有服务明确返回账号停用/删除时才永久停用邮箱。
    # 网络、CF、OTP 超时、登录页跳转或资料页暂时性错误均回收到 available，
    # 避免一次瞬时故障耗尽 iCloud/邮箱池。已确认落盘的账号仍由数据库状态保护，不会被回收。
    lowered = text.lower()
    return (
        "account_deactivated" in lowered
        or "account_deleted" in lowered
        or "account_banned" in lowered
        or "deleted or deactivated" in lowered
    )


def _disable_job_email(email: str | None, reason: str) -> bool:
    """把本次任务邮箱停用，避免后续再次领取。"""
    if not email:
        return False
    try:
        from core.email_provider import release_email

        source = release_email(email, status="disabled", note=f"自动停用: {reason[:180]}")
        logger.warning("[Service] 已自动停用邮箱: source=%s email=%s reason=%s", source, redact_email(email), reason[:220])
        return True
    except Exception:
        logger.exception("[Service] 自动停用邮箱失败: %s", redact_email(email))
        return False


def _normalize_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return _DEFAULT_MAX_WORKERS
    try:
        value = int(max_workers)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_WORKERS
    return max(_MIN_MAX_WORKERS, min(_MAX_MAX_WORKERS, value))


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回注册线程池。

    旧逻辑只在首次创建线程池时使用 max_workers，后续 WebUI 改线程数再提交仍会复用
    上一次的池。这里改成：每次传入的 max_workers 和当前池不一致时，立即创建新池供
    新提交任务使用；旧池不接收新任务，但会继续把已经排队/运行的任务跑完。
    """
    global _executor, _executor_workers, _executor_generation
    requested_workers = _normalize_workers(max_workers) if max_workers is not None else _executor_workers
    with _executor_lock:
        if _executor is None or requested_workers != _executor_workers:
            old_executor = _executor
            if old_executor is not None:
                # 不取消旧池里已提交的任务，只是不再往旧池追加新任务。
                old_executor.shutdown(wait=False, cancel_futures=False)
                _retired_executors.append(old_executor)
                logger.info(
                    "[Service] 注册线程池 workers 从 %s 切换为 %s；旧池继续处理已排队任务",
                    _executor_workers,
                    requested_workers,
                )
            _executor_workers = requested_workers
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"reg-worker-{_executor_generation}",
            )
    return _executor


def get_executor_workers() -> int:
    """当前新提交注册任务会使用的线程数。"""
    with _executor_lock:
        return _executor_workers


def shutdown_executor(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        executors = []
        if _executor is not None:
            executors.append(_executor)
            _executor = None
        executors.extend(_retired_executors)
        _retired_executors.clear()
    for ex in executors:
        ex.shutdown(wait=wait, cancel_futures=False)


# ============================================================
# 单任务执行：日志重定向到任务专属文件
# ============================================================

class _JobLogContext:
    """让本线程的根 logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        # Gunicorn 直接加载 app factory 时根 logger 默认是 WARNING；显式打开业务 INFO，
        # 让浏览器启动、代理轮换和页面阶段能实时写进任务日志。
        logging.getLogger("core").setLevel(logging.INFO)
        logging.getLogger("main").setLevel(logging.INFO)
        logging.getLogger(__name__).setLevel(logging.INFO)
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        # 仅给本线程过滤 —— 用 thread name 做区分，避免污染其他任务的日志
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


def _registration_process_entry(
    result_queue,
    log_file: str,
    email: str,
    name: str,
    birthday: str,
) -> None:
    """Run browser automation in a process that can be forcefully reaped."""
    try:
        os.setsid()
    except Exception:
        pass
    try:
        with _JobLogContext(log_file):
            from main import run_registration

            logging.getLogger(__name__).info("[注册子进程] 已启动，浏览器调用与 WebUI 进程隔离")
            result = run_registration(email=email, name=name, birthday=birthday)
        result_queue.put({"kind": "result", "result": result})
    except BaseException as exc:
        try:
            result_queue.put({
                "kind": "error",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            })
        except Exception:
            pass


def _run_registration_isolated(job_id: int, log_file: str, email: str, name: str, birthday: str) -> dict:
    """Wait for a registration child while keeping stop/timeout handling in the parent."""
    context = mp.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_registration_process_entry,
        args=(result_queue, log_file, email, name, birthday),
        name=f"registration-{job_id}",
    )
    process.daemon = False
    process.start()
    _register_active_registration_process(job_id, process)
    timeout = _job_timeout_seconds()
    deadline = time.monotonic() + timeout
    try:
        while True:
            job = db.get_job(job_id) or {}
            if is_stop_requested(job_id):
                # db.get_job exposes the persisted field as error_message.
                # Reading the old ``error`` key made watchdog timeouts look
                # like manual stops and selected the wrong terminal status.
                reason = str(job.get("error_message") or job.get("error") or "用户手动停止")
                _terminate_registration_process(process, reason)
                if reason.startswith("自动超时"):
                    raise RegistrationJobTimeout(reason)
                raise StopRequested(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = f"自动超时：注册子进程超过 {timeout} 秒，已终止并清理浏览器"
                _terminate_registration_process(process, reason)
                raise RegistrationJobTimeout(reason)
            try:
                message = result_queue.get(timeout=min(0.5, remaining))
            except Empty:
                if not process.is_alive():
                    process.join(timeout=1)
                    raise RuntimeError(
                        f"注册子进程异常退出（exitcode={process.exitcode}），任务已收敛"
                    )
                continue
            if not isinstance(message, dict):
                raise RuntimeError("注册子进程返回了无效结果")
            if message.get("kind") == "error":
                raise RuntimeError(str(message.get("error") or "注册子进程失败"))
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("注册子进程未返回有效注册结果")
            process.join(timeout=3)
            return result
    finally:
        _unregister_active_registration_process(job_id, process)
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass
        if process.is_alive():
            _terminate_registration_process(process, "父任务收尾")
        else:
            process.join(timeout=1)
        try:
            process.close()
        except Exception:
            pass


def _run_one_job_inner(job_id: int, log_file: str) -> None:
    """单任务入口（线程池里跑这个）。"""
    log_logger = logging.getLogger(__name__)
    _activate_job(job_id)

    # 取消检查：用户可能在任务排队期间点了"取消排队"，把 status 改成了 cancelled。
    # 因为 Future 已经 submit 进线程池无法撤回，只能在真正执行前自检一下，跳过 cancelled 的。
    current = db.get_job(job_id)
    if not current:
        log_logger.info(f"[Job {job_id}] 任务记录已删除，跳过执行")
        _deactivate_job(job_id)
        return
    if current.get("status") == "cancelled":
        log_logger.info(f"[Job {job_id}] 已被用户取消，跳过执行")
        _deactivate_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
    _append_job_log(job_id, "任务已进入运行状态，正在领取邮箱并启动浏览器；后续会逐条记录代理轮换和页面阶段。")
    watchdog = _start_job_watchdog(job_id)

    email: str | None = None
    try:
        with _JobLogContext(log_file):
            log_logger.info(f"[Job {job_id}] 开始注册任务")
            email, name, birthday = _prepare_registration_args()
            db.update_job(job_id, email=email)
            check_stop_requested()
            result = _run_registration_isolated(job_id, log_file, email, name, birthday)
            if is_stop_requested(job_id):
                _release_unconsumed_job_email(email, "用户手动停止")
                db.update_job(
                    job_id,
                    status="stopped",
                    error="用户手动停止",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.warning(f"[Job {job_id}] 已按用户请求停止")
                return
            if isinstance(result, dict) and result.get("success"):
                db.update_job(
                    job_id,
                    status="success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.info("[Job %s] 成功: %s", job_id, redact_email(result.get("email")))
            else:
                # 注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）
                err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
                result_email = (result or {}).get("email") if isinstance(result, dict) else None
                db.update_job(
                    job_id,
                    status="failed",
                    email=result_email,
                    account_id=(result or {}).get("account_id") if isinstance(result, dict) else None,
                    error=str(err)[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                email_to_handle = str(result_email or email or "").strip()
                if _should_disable_failed_registration_email(err):
                    _disable_job_email(email_to_handle, str(err))
                else:
                    _release_unconsumed_job_email(email_to_handle, str(err))
                log_logger.error(f"[Job {job_id}] 失败: {err}")
    except RegistrationJobTimeout as exc:
        _release_unconsumed_job_email(email, str(exc))
        log_logger.error(f"[Job {job_id}] 自动超时并已清理子进程: {exc}")
        db.update_job(
            job_id,
            status="failed",
            error=str(exc)[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except StopRequested as exc:
        _release_unconsumed_job_email(email, str(exc))
        current = db.get_job(job_id) or {}
        reason = str(current.get("error_message") or str(exc))
        auto_timeout = reason.startswith("自动超时") or str(exc).startswith("自动超时")
        terminal_status = "failed" if auto_timeout else "stopped"
        terminal_error = reason[:500] if auto_timeout else "用户手动停止"
        log_logger.warning(f"[Job {job_id}] {'自动超时' if auto_timeout else '已停止'}: {reason}")
        db.update_job(
            job_id,
            status=terminal_status,
            error=terminal_error,
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}"
        if _should_disable_failed_registration_email(err_text):
            _disable_job_email(email, err_text)
        else:
            _release_unconsumed_job_email(email, err_text)
        if is_stop_requested(job_id):
            current = db.get_job(job_id) or {}
            reason = str(current.get("error_message") or str(exc))
            auto_timeout = reason.startswith("自动超时")
            terminal_status = "failed" if auto_timeout else "stopped"
            terminal_error = reason[:500] if auto_timeout else "用户手动停止"
            log_logger.warning(
                f"[Job {job_id}] {'自动超时' if auto_timeout else '停止'}中捕获异常，收敛任务: "
                f"{type(exc).__name__}: {exc}"
            )
            db.update_job(
                job_id,
                status=terminal_status,
                error=terminal_error,
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return
        log_logger.exception(f"[Job {job_id}] 异常")
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        watchdog.cancel()
        _deactivate_job(job_id)


def _run_one_job(job_id: int, log_file: str) -> None:
    """真实注册工作单元：取消预检后，领取邮箱前占用全局流水线槽位。"""
    current = db.get_job(job_id)
    if not current:
        logger.info("[Job %s] 任务记录已删除，跳过执行", job_id)
        return
    if current.get("status") == "cancelled":
        logger.info("[Job %s] 已被用户取消，跳过执行", job_id)
        return

    # inner 内会再次做取消检查，覆盖排队等待槽位期间发生的取消。
    # 注册完成只负责非阻塞地把首测/套餐查询加入队列，不等待下游，避免嵌套占槽死锁。
    # 先等浏览器资源槽，再占用全局流水线槽；排队任务不会阻塞其它轻量后台任务。
    try:
        with _registration_browser_slot(job_id):
            with pipeline_slot("registration"):
                return _run_one_job_inner(job_id, log_file)
    except StopRequested as exc:
        now = datetime.now().isoformat(timespec="seconds")
        db.update_job(job_id, status="stopped", error="用户手动停止", completed_at=now)
        _append_job_log(job_id, f"任务在等待浏览器资源槽时停止：{exc}")
        logger.info("[Job %s] %s", job_id, exc)


def _run_codex_retry_job(job_id: int, log_file: str, email: str, account_id: int) -> None:
    """把 Codex 补跑作为标准任务执行，并复用任务状态、日志和停止入口。"""
    _activate_job(job_id)
    current = db.get_job(job_id)
    if not current or current.get("status") == "cancelled":
        codex_retry_service.release(email)
        _deactivate_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        result = codex_retry_service.run_worker(
            email,
            clear_log=False,
            target_log_path=log_file,
        )
        now_iso = datetime.now().isoformat(timespec="seconds")
        if is_stop_requested(job_id) or result.get("status") == "stopped":
            db.update_job(job_id, status="stopped", email=email, account_id=account_id, error=str(result.get("message") or "用户手动停止")[:500], completed_at=now_iso)
        elif result.get("ok"):
            db.update_job(
                job_id,
                status="success",
                email=email,
                account_id=account_id,
                completed_at=now_iso,
            )
        else:
            db.update_job(
                job_id,
                status="failed",
                email=email,
                account_id=account_id,
                error=str(result.get("message") or "Codex 补跑失败")[:500],
                completed_at=now_iso,
            )
    except Exception as exc:
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        codex_retry_service.release(email)
        logger.exception("[Job %s] Codex 补跑异常", job_id)
    finally:
        _deactivate_job(job_id)


# ============================================================
# 公共接口
# ============================================================

def submit_registration(count: int = 1, email_source: str | None = None, workers: int | None = None) -> list[dict]:
    """
    创建 N 个注册任务并提交到线程池。
    email_source 仅记录到 DB；实际邮箱来源固定为 Outlook 账号池。

    Returns:
        N 个新创建的 job dict
    """
    if email_source is None:
        from config import email as _email_cfg
        email_source = _email_cfg.EMAIL_SOURCE

    # 创建/切换线程池和提交本批任务必须整体串行化：否则另一请求在本批提交中途
    # 切换 workers 并 shutdown 旧池，会导致后续 submit 报 cannot schedule new futures after shutdown。
    with _executor_lock:
        executor = get_executor(max_workers=workers)
        effective_workers = get_executor_workers()
        jobs = []
        for _ in range(count):
            job = db.create_job(email_source=email_source)
            try:
                executor.submit(_run_one_job, job["id"], job["log_file"])
            except Exception as exc:
                db.update_job(
                    int(job["id"]),
                    status="failed",
                    error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                logger.exception("[Service] 注册任务 #%s 提交线程池失败", job["id"])
            jobs.append(db.get_job(int(job["id"])) or job)
    logger.info(f"[Service] 已提交 {count} 个注册任务，源={email_source}，workers={effective_workers}")
    return jobs


def _account_for_job(job: dict) -> dict | None:
    account_id = job.get("account_id")
    if account_id is not None:
        try:
            account = db.get_account(int(account_id))
            if account is not None:
                return account
        except (TypeError, ValueError):
            pass
    email = str(job.get("email") or "").strip()
    return db.get_account_by_email(email) if email else None


def get_retry_info(job: dict) -> dict:
    """返回给 API/UI 的重试能力描述，不依赖前端猜测错误阶段。"""
    status = str(job.get("status") or "")
    info = {
        "retryable": False,
        "retry_action": None,
        "retry_label": None,
        "retry_reason": None,
        "display_status": status,
    }
    if status not in ("failed", "stopped", "cancelled"):
        return info

    successful_retry = db.get_successful_retry_for_job(int(job.get("id") or 0))
    if successful_retry is not None:
        info["retry_reason"] = f"后续重试任务 #{successful_retry.get('id')} 已成功"
        info["successful_retry_job_id"] = successful_retry.get("id")
        return info

    account = _account_for_job(job)
    if account and job.get("account_id") is not None and status in ("failed", "stopped"):
        info["display_status"] = "success" if (account.get("codex_status") or "") == "success" else "partial_success"

    if account:
        codex_status = str(account.get("codex_status") or "")
        if codex_status == "deactivated":
            info["retry_reason"] = "账号已废号，不能补跑 Codex"
            return info
        if codex_status == "success":
            info["retry_reason"] = "账号和 Codex 授权均已完成"
            return info
        info.update({
            "retryable": True,
            "retry_action": "codex",
            "retry_label": "补跑 Codex",
        })
        return info

    info.update({
        "retryable": True,
        "retry_action": "registration",
        "retry_label": "重试",
    })
    return info


def retry_job(job_id: int, workers: int | None = None) -> dict:
    """智能重试终态任务：未生成账号则重新注册，已有账号则仅补跑 Codex。"""
    source = db.get_job(job_id)
    if source is None:
        return {"ok": False, "error": "任务不存在", "status": 404}

    retry_info = get_retry_info(source)
    if not retry_info["retryable"]:
        reason = retry_info.get("retry_reason") or f"当前状态不支持重试：{source.get('status')}"
        return {"ok": False, "error": reason, "status": 409}

    action = str(retry_info["retry_action"])
    account = _account_for_job(source)
    email = str((account or {}).get("email") or source.get("email") or "").strip()
    account_id = int(account["id"]) if account and account.get("id") is not None else None
    reserved_codex = False
    if action == "codex":
        if not email or account_id is None:
            return {"ok": False, "error": "已注册账号信息不完整，无法补跑 Codex", "status": 409}
        if not codex_retry_service.reserve(email):
            return {"ok": False, "error": "该账号正在补跑 Codex，请稍候", "status": 409}
        reserved_codex = True

    try:
        job, created = db.create_retry_job(
            int(job_id),
            job_type="codex_retry" if action == "codex" else "registration",
            email_source=str(source.get("email_source") or "outlook"),
            email=email if action == "codex" else None,
            account_id=account_id if action == "codex" else None,
        )
    except LookupError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 404}
    except ValueError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 409}

    if not created:
        if reserved_codex:
            codex_retry_service.release(email)
        return {
            "ok": True,
            "created": False,
            "reused": True,
            "message": f"已有重试任务 #{job['id']} 在排队或运行中",
            "source_job_id": int(job_id),
            "retry_action": action,
            "job": job,
        }

    try:
        if action == "codex":
            db.update_account_codex_status(email, "retrying", None)
        with _executor_lock:
            executor = get_executor(max_workers=workers)
            if action == "codex":
                executor.submit(_run_codex_retry_job, job["id"], job["log_file"], email, int(account_id))
            else:
                executor.submit(_run_one_job, job["id"], job["log_file"])
    except Exception as exc:
        if reserved_codex:
            codex_retry_service.release(email)
            db.update_account_codex_status(email, "failed", f"队列提交失败：{type(exc).__name__}: {exc}"[:500])
        db.update_job(
            int(job["id"]),
            status="failed",
            error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        logger.exception("[Service] 重试任务 #%s 提交线程池失败", job["id"])
        return {"ok": False, "error": "重试任务创建成功，但提交执行失败", "status": 500, "job": db.get_job(int(job["id"]))}

    return {
        "ok": True,
        "created": True,
        "reused": False,
        "message": f"已创建重试任务 #{job['id']}（{'Codex 补跑' if action == 'codex' else '完整注册'}）",
        "source_job_id": int(job_id),
        "retry_action": action,
        "job": job,
    }


def cancel_pending_jobs() -> int:
    """
    把所有 status=pending 的任务批量改成 cancelled，避免它们被执行。
    已经在 running 的任务不动（线程池中无法中途打断）。
    返回成功取消的数量。

    实际"不执行"的保证在 _run_one_job 开头——它真要跑起来时会先看 status 决定是否跳过。
    """
    jobs = db.list_jobs(limit=1000)
    cancelled = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    for job in jobs:
        if job.get("status") == "pending":
            db.update_job(
                int(job["id"]),
                status="cancelled",
                completed_at=now_iso,
                error="用户手动取消",
            )
            cancelled += 1
    logger.info(f"[Service] 已取消 {cancelled} 个排队任务")
    return cancelled


def request_stop_job(job_id: int) -> dict:
    """手动停止单个注册任务。pending 直接取消；running 设置停止标记，运行线程会在检查点退出。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在", "status": 404}
    status = job.get("status")
    now_iso = datetime.now().isoformat(timespec="seconds")
    if status == "pending":
        db.update_job(job_id, status="cancelled", completed_at=now_iso, error="用户手动停止/取消排队")
        _append_job_log(job_id, "用户手动停止：任务尚未运行，已取消排队。")
        return {"ok": True, "message": "排队任务已取消", "job_id": job_id, "state": "cancelled"}
    if status in ("success", "failed", "cancelled", "stopped"):
        return {"ok": True, "message": f"任务已结束：{status}", "job_id": job_id, "state": status}
    if status in ("running", "stopping"):
        with _STOP_LOCK:
            active = int(job_id) in _ACTIVE_JOBS
            ev = _STOP_EVENTS.get(int(job_id)) if active else None
            if ev is not None:
                ev.set()
        if not active or ev is None:
            # Web 服务重启、线程异常退出、历史残留 stopping，或之前手动停止时只创建了 stop event
            # 但没有真实线程实例：直接落为 stopped，避免永远卡在“停止中”。
            with _STOP_LOCK:
                _STOP_EVENTS.pop(int(job_id), None)
                _ACTIVE_JOBS.discard(int(job_id))
            db.update_job(
                job_id,
                status="stopped",
                completed_at=now_iso,
                error="用户手动停止（任务实例不存在）",
            )
            _release_unconsumed_job_email(
                str(job.get("email") or "").strip() or None,
                "任务实例不存在，确认未继续执行",
            )
            _append_job_log(job_id, "用户手动停止：未找到运行中的任务实例，已直接标记为已停止。")
            logger.warning("[Service] 用户停止任务 #%s：任务实例不存在，已直接标记 stopped", job_id)
            return {"ok": True, "message": "任务实例不存在，已直接标记为已停止", "job_id": job_id, "state": "stopped"}
        db.update_job(job_id, status="stopping", error="用户手动停止中")
        _append_job_log(job_id, "用户手动停止：已发送停止信号，任务会在当前步骤检查点退出。")
        logger.warning("[Service] 用户请求停止任务 #%s", job_id)
        return {"ok": True, "message": "已发送停止信号", "job_id": job_id, "state": "stopping"}
    return {"ok": False, "error": f"当前状态不支持停止：{status}", "status": 409}


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志文件最后 max_bytes 字节，给 Web UI 显示。"""
    job = db.get_job(job_id)
    if not job or not job.get("log_file"):
        return ""
    p = Path(job["log_file"])
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")
