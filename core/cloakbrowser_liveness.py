# -*- coding: utf-8 -*-
"""使用 CloakBrowser 完成已注册 ChatGPT 账号的邮箱 OTP 查活。"""
from __future__ import annotations

import logging
import time

from config import cloakbrowser as cloak_cfg
from config import email as email_cfg
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import OtpWaitSession, wait_for_otp
from core.humanize import delay as human_delay
from core.log_safety import redact_email
from core.openai_auth import AccountUnusableError
from core.roxy_registration import (
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
    _fill_password_page_if_present,
    _is_chatgpt_logged_in_page,
    _is_email_verification_page,
    _maybe_accept,
    _pass_mfa_challenge_if_needed,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
    _recover_otp_error_page,
    _fetch_chatgpt_session,
)

logger = logging.getLogger(__name__)


def _setting_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(getattr(cloak_cfg, name, default) or default))
    except (TypeError, ValueError):
        return default


def _session_result(session_info: dict, *, proxy: str | None, opened) -> dict:
    access_token = str(session_info.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("Cloak 登录成功但未拿到 accessToken")
    raw = opened.raw if opened is not None and isinstance(opened.raw, dict) else {}
    return {
        "ok": True,
        "status": "live",
        "method": "otp",
        "access_token": access_token,
        "session": session_info,
        "device_id": raw.get("device_id") or "",
        "proxy_used": raw.get("proxy") or proxy or None,
        "proxy_mode": raw.get("proxy_mode") or "cloak",
        "proxy_group": raw.get("proxy_group") or "",
        "proxy_node": raw.get("proxy_node") or "",
    }


def run_cloak_liveness_flow(
    email: str,
    *,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
) -> dict:
    """通过 CloakBrowser 对现有账号执行一次邮箱 OTP 登录并刷新 AT。"""
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    driver = None
    opened = None
    try:
        from core.cloakbrowser_driver import account_fingerprint_seed

        driver, opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=account_fingerprint_seed(email),
        )
        login_timeout = _setting_int("CLOAK_LOGIN_PAGE_TIMEOUT", 25, 8)
        normal_timeout = _setting_int("CLOAK_SELENIUM_TIMEOUT", 90, 20)
        driver.set_page_load_timeout(login_timeout)
        try:
            driver.get("https://chatgpt.com/auth/login")
        finally:
            driver.set_page_load_timeout(normal_timeout)
        _maybe_accept(driver)
        human_delay("navigate")

        email_timeout = _setting_int("CLOAK_EMAIL_STEP_TIMEOUT", 120, 20)
        _submit_email_and_wait_next(driver, email, attempts=2, timeout=email_timeout, allow_password_page=True)
        _fill_password_page_if_present(
            driver,
            email,
            timeout=_setting_int("CLOAK_PASSWORD_PAGE_TIMEOUT", 45, 15),
        )

        if not _is_email_verification_page(driver) and _is_chatgpt_logged_in_page(driver):
            session_info = _fetch_chatgpt_session(
                driver,
                timeout=_setting_int("CLOAK_SESSION_TIMEOUT", 90, 30),
            )
            return _session_result(session_info, proxy=proxy, opened=opened)

        otp_wait_session = OtpWaitSession(wait_fn=wait_for_otp)
        otp_after_ts = time.time()
        current_otp = None
        resend_count = 0
        otp_wait = max(15, int(getattr(email_cfg, "OTP_SINGLE_WAIT", 75) or 75))
        otp_submit_timeout = _setting_int("CLOAK_OTP_SUBMIT_TIMEOUT", 20, 8)
        for attempt in range(1, 4):
            if current_otp is None:
                logger.info(
                    "[Cloak查活][OTP] 等待验证码：%s（第 %s/3 次）",
                    redact_email(email),
                    attempt,
                )
                try:
                    current_otp = otp_wait_session.wait(email, after_ts=otp_after_ts, max_wait=otp_wait)
                except TimeoutError as exc:
                    logger.warning(
                        "[Cloak查活][OTP] 第 %s 次等待验证码超时，准备复用一次重发机会：%s",
                        attempt,
                        type(exc).__name__,
                    )
                    # 邮件接口超时与验证码错误一样，都需要给新邮件一次机会；
                    # 但如果浏览器已经跳回 ChatGPT，优先信任登录态。
                    if _is_chatgpt_logged_in_page(driver):
                        logger.info("[Cloak查活][OTP] 等待取码期间已检测到 ChatGPT 首页，按成功继续")
                        break
                    if resend_count >= 2:
                        raise RuntimeError("邮箱验证码等待超时，重发机会已用尽") from exc
                    resend_count += 1
                    resend_result = _click_resend_email_otp(driver, timeout=25)
                    if resend_result.get("reason") == "otp_page_left":
                        break
                    otp_after_ts = time.time()
                    continue
            otp_wait_session.mark_used(current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                # 页面可能已自动提交/跳转；继续观察页面状态而不是直接判死。
                logger.info("[Cloak查活][OTP] 未找到显式提交按钮，继续等待页面状态：%s", type(exc).__name__)
            outcome = _wait_after_email_otp_submit(driver, timeout=otp_submit_timeout)
            logger.info("[Cloak查活][OTP] 提交结果：%s", outcome)
            if outcome == "error_page":
                # 服务端路由错误页：恢复后重交一次；仍失败则按停滞处理（进入重发/重试分支）。
                logger.warning("[Cloak查活][OTP] 提交后进入服务端错误页，尝试恢复后重交")
                if _recover_otp_error_page(driver):
                    _clear_otp_inputs(driver)
                    _type_otp(driver, current_otp)
                    human_delay("otp_input")
                    try:
                        _click_continue(driver)
                    except Exception as exc:
                        logger.info("[Cloak查活][OTP] 错误页恢复后未找到提交按钮：%s", type(exc).__name__)
                    outcome = _wait_after_email_otp_submit(driver, timeout=otp_submit_timeout)
                    logger.info("[Cloak查活][OTP] 错误页恢复后提交结果：%s", outcome)
                if outcome == "error_page":
                    outcome = "stalled"
            if outcome == "accepted":
                if _pass_mfa_challenge_if_needed(driver, email, timeout=max(otp_submit_timeout, 25)):
                    logger.info("[Cloak查活][OTP] 2FA 动态码已通过，继续读取登录态")
                break
            if _is_chatgpt_logged_in_page(driver):
                logger.info("[Cloak查活][OTP] 提交结果为 %s 但页面已登录，跳过重发并继续", outcome)
                break
            if outcome == "invalid" and attempt >= 3:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            if outcome == "stalled" and attempt >= 3:
                raise RuntimeError("OTP 提交后页面停滞，已达到最大重试次数")
            if resend_count < 2:
                resend_count += 1
                resend_result = _click_resend_email_otp(driver, timeout=25)
                if resend_result.get("reason") == "otp_page_left":
                    break
                otp_after_ts = time.time()
            current_otp = None
        else:
            raise RuntimeError("邮箱验证码登录未完成")

        session_info = _fetch_chatgpt_session(
            driver,
            timeout=_setting_int("CLOAK_SESSION_TIMEOUT", 90, 30),
        )
        return _session_result(session_info, proxy=proxy, opened=opened)
    except AccountUnusableError:
        raise
    except Exception as exc:
        if "account_deactivated" in str(exc).lower() or "deleted or deactivated" in str(exc).lower():
            raise AccountUnusableError(str(exc), error_code="account_deactivated") from exc
        raise
    finally:
        if driver is not None and not bool(getattr(cloak_cfg, "CLOAK_KEEP_BROWSER_OPEN", False)):
            try:
                driver.quit()
            except Exception:
                logger.debug("[Cloak查活] 关闭浏览器失败", exc_info=True)
