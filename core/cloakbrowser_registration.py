# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from config import cloakbrowser as _cfg
from config import email as _email_cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import OtpWaitSession, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.log_safety import redact_email, redact_emails

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _is_chatgpt_logged_in_page,
)

logger = logging.getLogger(__name__)


def _register_active_browser(driver) -> None:
    """登记当前任务浏览器；独立注册子进程中没有父线程上下文时安全跳过。"""
    try:
        from core import registration_service as _service
        job_id = getattr(_service._THREAD_CTX, "job_id", None)
        if job_id:
            _service.register_active_browser(int(job_id), driver)
    except Exception:
        pass


def _unregister_active_browser(driver) -> None:
    """注销当前任务浏览器，兼容父进程登记和独立子进程收尾。"""
    try:
        from core import registration_service as _service
        job_id = getattr(_service._THREAD_CTX, "job_id", None)
        if job_id:
            _service.unregister_active_browser(int(job_id), driver)
    except Exception:
        pass


def _close_driver_async(driver, label: str) -> None:
    def _close() -> None:
        try:
            driver.quit()
            logger.warning("[Cloak注册] 阶段超时，已关闭浏览器：%s", label)
        except Exception as exc:
            logger.warning("[Cloak注册] 阶段超时关闭浏览器失败：%s: %s", label, exc)

    threading.Thread(target=_close, name="cloak-stage-stop", daemon=True).start()


@contextmanager
def _stage_deadline(driver, label: str, seconds: int):
    """Give synchronous browser calls a wall-clock deadline independent of Selenium."""
    expired = threading.Event()
    timeout = max(1, int(seconds))

    def _expire() -> None:
        expired.set()
        logger.warning("[Cloak注册] 阶段超过 %ss：%s，主动关闭浏览器并结束当前任务", timeout, label)
        _close_driver_async(driver, label)

    timer = threading.Timer(timeout, _expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
    if expired.is_set():
        raise TimeoutError(f"execution_stage_timeout: {label} > {timeout}s")


_PROXY_REJECTION_MARKERS = (
    "just a moment",
    "しばらくお待ちください",
    "checking your browser",
    "cloudflare",
    # 页面流程错误（例如 authorize 重定向后状态读取滞后）不属于代理拒绝，
    # 不应因此重启多个浏览器并重复消耗同一邮箱。
    "err_proxy_connection_failed",
    "proxyerror",
    "net::err_",
    "page.goto: timeout",
)


def is_proxy_rejection_error(error: object) -> bool:
    """识别应立即更换代理的 ChatGPT/Cloudflare 页面拒绝。"""
    text = str(error or "").lower()
    if any(marker in text for marker in _PROXY_REJECTION_MARKERS):
        return True
    # OAuth/代理客户端经常只把 HTTP 状态和拦截原因写入异常，未带页面标题。
    # 仅对 403/407/429/5xx 与代理、挑战或拒绝语义组合判定，避免把账号业务错误
    # （例如 account_deactivated）误交给代理轮换。
    status_hit = re.search(r"(?<!\d)(?:403|407|429|5\d\d)(?!\d)", text)
    proxy_context = (
        "proxy", "forbidden", "access denied", "challenge", "cf-ray",
        "intercept", "blocked", "拦截", "代理", "风控",
    )
    return bool(status_hit and any(marker in text for marker in proxy_context))


def _assert_login_gate_not_blocked(driver) -> None:
    """登录页出现明确 Cloudflare 等待页时快速失败，交给上层轮换代理。"""
    try:
        state = driver.execute_script("""
        return {
          title: document.title || '',
          text: (document.body?.innerText || '').slice(0, 1200),
          emailInputs: [...document.querySelectorAll('input')].filter(el => {
            const a = [el.type, el.name, el.id, el.autocomplete].join(' ').toLowerCase();
            return /email|username/.test(a);
          }).length
        };
        """) or {}
    except Exception:
        return
    if not isinstance(state, dict):
        return
    page_text = f"{state.get('title') or ''}\n{state.get('text') or ''}".lower()
    markers = ("just a moment", "しばらくお待ちください", "checking your browser", "cloudflare")
    try:
        email_inputs = int(state.get("emailInputs") or 0)
    except (TypeError, ValueError):
        email_inputs = 0
    if not email_inputs and any(marker in page_text for marker in markers):
        raise RuntimeError(f"ChatGPT/Cloudflare 拦截当前代理，准备轮换；title={state.get('title') or '-'}")


def run_cloak_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    *,
    defer_email_release: bool = False,
    proxy_selection: dict | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy, proxy_selection=proxy_selection)
        logger.info("[Cloak注册] 开始：%s，profile=%s", redact_email(email), opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        normal_timeout = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90)
        login_timeout = max(8, min(normal_timeout, int(getattr(_cfg, "CLOAK_LOGIN_PAGE_TIMEOUT", 25) or 25)))
        with _stage_deadline(driver, "浏览器启动/登录页", login_timeout):
            driver.set_page_load_timeout(login_timeout)
            try:
                driver.get("https://chatgpt.com/auth/login")
            finally:
                driver.set_page_load_timeout(normal_timeout)
            human_delay("navigate")
            _assert_login_gate_not_blocked(driver)
            _maybe_accept(driver)
            _check_manual_stop()

        # 相同出口提交失败后继续在同一页面重试意义很小；单次确认失败即交给上层换代理。
        email_step_timeout = max(20, int(getattr(_cfg, "CLOAK_EMAIL_STEP_TIMEOUT", 120) or 120))
        with _stage_deadline(driver, "登录页进入下一步", email_step_timeout):
            next_state = _submit_email_and_wait_next(driver, email, attempts=1, timeout=email_step_timeout)
            _check_manual_stop()

        password_timeout = max(15, int(getattr(_cfg, "CLOAK_PASSWORD_PAGE_TIMEOUT", 45) or 45))
        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=password_timeout)
        _check_manual_stop()

        current_otp = otp_code
        otp_wait_session = OtpWaitSession(wait_fn=wait_for_otp)
        otp_single_wait = max(15, int(getattr(_email_cfg, "OTP_SINGLE_WAIT", 75) or 75))
        max_otp_attempts = 3
        resend_used = False
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", redact_email(email), otp_attempt, max_otp_attempts)
                try:
                    current_otp = otp_wait_session.wait(email, after_ts=otp_after_ts, max_wait=otp_single_wait)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，继续下一轮等待（%s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        redact_emails(exc)[:180],
                    )
                    if not resend_used:
                        resend_used = True
                        otp_after_ts = time.time()
                        resend_result = _click_resend_email_otp(driver, timeout=25)
                        if isinstance(resend_result, dict) and resend_result.get("reason") == "otp_page_left":
                            break
                        human_delay("api")
                    else:
                        logger.info("[Cloak注册][OTP] 本任务已使用一次重发，不再重复发送")
                    current_otp = None
                    continue
            otp_wait_session.mark_used(current_otp)
            logger.info("[Cloak注册][OTP] 已收到验证码，code_len=%s", len(str(current_otp or "")))
            _check_manual_stop()
            logger.info("[Cloak注册][OTP] 开始填写验证码")
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            logger.info("[Cloak注册][OTP] 验证码填写完成，准备提交")
            human_delay("otp_input")
            _check_manual_stop()
            try:
                _click_continue(driver)
                logger.info("[Cloak注册][OTP] 已点击提交，等待页面离开验证码页")
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", redact_emails(exc)[:120])

            otp_submit_timeout = max(8, int(getattr(_cfg, "CLOAK_OTP_SUBMIT_TIMEOUT", 20) or 20))
            outcome = _wait_after_email_otp_submit(driver, timeout=otp_submit_timeout)
            _check_manual_stop()
            if outcome == "stalled":
                logger.warning("[Cloak注册][OTP] 首次提交没有产生页面状态变化，使用同一验证码重提一次")
                _click_continue(driver)
                logger.info("[Cloak注册][OTP] 已完成第二次提交，等待页面状态")
                outcome = _wait_after_email_otp_submit(driver, timeout=otp_submit_timeout)
                _check_manual_stop()
                if outcome == "stalled":
                    raise RuntimeError("OTP 提交两次后仍停留在验证码页，结束当前代理并轮换")
            if outcome == "accepted":
                break
            # callback 可能在返回 outcome 后才完成；已到 ChatGPT 首页时禁止
            # 再点击验证码重发，直接进入后续 session 读取。
            if _is_chatgpt_logged_in_page(driver):
                logger.info("[Cloak注册][OTP] 已进入 ChatGPT 首页，跳过验证码重发")
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            if not resend_used:
                resend_used = True
                otp_after_ts = time.time()
                resend_result = _click_resend_email_otp(driver, timeout=25)
                if isinstance(resend_result, dict) and resend_result.get("reason") == "otp_page_left":
                    break
                human_delay("api")
            else:
                logger.info("[Cloak注册][OTP] 本任务已使用一次重发，不再重复发送")
            current_otp = None

        profile_timeout = max(30, int(getattr(_cfg, "CLOAK_PROFILE_TIMEOUT", 90) or 90))
        with _stage_deadline(driver, "资料页提交", profile_timeout):
            profile_submitted = _complete_profile_page(driver, name, birthday, timeout=profile_timeout)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_timeout = max(30, int(getattr(_cfg, "CLOAK_SESSION_TIMEOUT", 90) or 90))
        with _stage_deadline(driver, "登录态/session", session_timeout):
            session_info = _fetch_chatgpt_session(driver, timeout=session_timeout)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", redact_email(email))

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Cloak注册] 当前 CloakBrowser 自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_proxy": {
                    "mode": ((opened.raw or {}).get("proxy_mode") if opened else "") or "",
                    "group": ((opened.raw or {}).get("proxy_group") if opened else "") or "",
                    "node_name": ((opened.raw or {}).get("proxy_node") if opened else "") or "",
                    "exit_ip": ((opened.raw or {}).get("proxy_exit_ip") if opened else "") or "",
                    "selection_ms": ((opened.raw or {}).get("proxy_selection_ms") if opened else 0) or 0,
                },
                "registration_password": openai_password,
                "codex": codex_result,
            },
        )
        try:
            from core.email_provider import release_email
            release_email(email, status="used", note="Cloak registration completed")
        except Exception as exc:
            logger.warning(
                "[Cloak registration] Failed to finalize email pool state: %s: %s: %s",
                redact_email(email),
                type(exc).__name__,
                redact_emails(exc),
            )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {"success": bool(codex_ok), "email": email, "account_id": account_id, "access_token": access_token, "totp_secret": totp_secret, "codex": codex_result, "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"}
    except Exception as exc:
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, redact_emails(exc))
        logger.debug("[Cloak注册] 失败详情\n%s", redact_emails(traceback.format_exc()))
        error_text = str(exc)
        lowered_error = error_text.lower()
        deactivated_email = any(marker in lowered_error for marker in (
            "account_deactivated",
            "deleted or deactivated",
        ))
        otp_delivery_timeout = "otp 总等待已达到" in lowered_error
        terminal_email = deactivated_email or otp_delivery_timeout
        # Cloudflare/代理拒绝由上层立即换出口并继续使用同一邮箱；最终耗尽时上层统一释放。
        if not defer_email_release and not is_proxy_rejection_error(exc):
            try:
                from core.email_provider import release_email
                release_email(
                    email,
                    status="failed" if (create_acknowledged or terminal_email) else "available",
                    note=f"Cloak注册失败: {error_text[:180]}",
                )
            except Exception:
                pass
        if deactivated_email:
            error = f"account_deactivated: {type(exc).__name__}: {error_text[:260]}"
        elif otp_delivery_timeout:
            error = f"otp_delivery_timeout: {type(exc).__name__}: {error_text[:260]}"
        else:
            error = f"{type(exc).__name__}: {error_text[:300]}"
        return {"success": False, "email": email, "error": error}
    finally:
        _unregister_active_browser(driver)
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
