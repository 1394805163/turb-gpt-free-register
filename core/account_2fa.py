# -*- coding: utf-8 -*-
"""给已注册账号补设 2FA（TOTP）：OTP 登录 -> reauth(密码链，实为邮箱 OTP) -> mfa enroll/activate。

reauth 请求带 connection=password + reauth=password，实际验证走邮箱 OTP；
token 里 pwd_auth_time 的新鲜度决定 enroll 是否被接受（沿用注册链路的 setup_2fa）。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_DRIVER_WATCHDOG_SECONDS = 600.0


def _start_driver_watchdog(driver, *, seconds: float = _DRIVER_WATCHDOG_SECONDS):
    """WebDriver 命令无超时；卡死时到点强退浏览器，让主流程抛错走失败分支。"""
    import threading

    def _force_quit():
        try:
            logger.warning("[补密码/2FA] 浏览器看门狗超时（%.0fs），强制退出被卡住的会话", seconds)
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass

    timer = threading.Timer(max(60.0, float(seconds)), _force_quit)
    timer.daemon = True
    timer.start()
    return timer


def _stored_password(email: str) -> str:
    from core import db

    acc = db.get_account_by_email(email) or {}
    return str(acc.get("password") or acc.get("registration_password") or "").strip()


def _submit_login_password(driver, password: str, timeout: int = 15) -> bool:
    """登录密码页兜底：没有"一次性验证码"入口时直接填 DB 密码并提交。"""
    try:
        element = _find_any(driver, [
            "input[type='password']",
            "input[name*='password' i]",
            "input[autocomplete='current-password']",
        ], timeout=timeout)
    except Exception:
        element = None
    if not element:
        return False
    try:
        _human_type_text(driver, element, str(password), clear=True)
        time.sleep(1)
        _click_continue(driver)
        return True
    except Exception:
        return False


def set_account_2fa(
    email: str,
    *,
    save: bool = True,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
) -> dict:
    """给已注册账号补设 2FA（TOTP），返回 {ok, status, email, totp_secret?, error?}。"""
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "status": "skipped", "error": "email 为空"}

    from core.account_password import _resolve_fresh_account_route
    from core.cloakbrowser_driver import account_fingerprint_seed, build_cloak_driver
    from core.email_provider import OtpWaitSession, wait_for_otp
    from core.page_session import PageSession
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _fetch_chatgpt_session,
        _fill_password_page_if_present,
        _find_any,
        _human_type_text,
        _is_email_verification_page,
        _is_login_password_page,
        _maybe_accept,
        _submit_email_and_wait_next,
        _type_otp,
        _wait_after_email_otp_submit,
    )

    if not proxy and not proxy_selection:
        proxy, proxy_selection = _resolve_fresh_account_route(email)

    driver = None
    try:
        driver, opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=account_fingerprint_seed(email),
        )
        driver.set_page_load_timeout(90)
        logger.info("[补2FA] 启动：%s profile=%s", email, opened.profile_id)
        _watchdog_timer = _start_driver_watchdog(driver)

        # ---- 1) 登录：邮箱 OTP；账号已设密码时走"一次性验证码入口"或密码兜底 ----
        driver.get("https://chatgpt.com/auth/login")
        _maybe_accept(driver)
        time.sleep(2)
        login_otp_after = time.time()
        next_state = _submit_email_and_wait_next(
            driver, email, attempts=2, timeout=120, allow_password_page=True
        )
        if next_state == "login_password":
            logger.info("[补2FA] 进入登录密码页：优先切换一次性验证码入口")
            _fill_password_page_if_present(driver, email, timeout=45)
            if _is_login_password_page(driver):
                stored_password = _stored_password(email)
                if not stored_password:
                    raise RuntimeError("账号已设密码且无一次性验证码入口，DB 中也缺少密码")
                logger.info("[补2FA] 未找到一次性验证码入口，改用 DB 密码登录")
                _submit_login_password(driver, stored_password)
                time.sleep(4)
        if _is_email_verification_page(driver):
            login_session = OtpWaitSession(wait_fn=wait_for_otp)
            login_code = login_session.wait(email, after_ts=login_otp_after, max_wait=90)
            logger.info("[补2FA] 登录验证码已收到（len=%s），提交", len(str(login_code or "")))
            _clear_otp_inputs(driver)
            _type_otp(driver, login_code)
            _click_continue(driver)
            outcome = _wait_after_email_otp_submit(driver, timeout=25)
            logger.info("[补2FA] 登录验证码提交结果：%s", outcome)
        time.sleep(2)
        info = _fetch_chatgpt_session(driver, timeout=90)
        if not info.get("accessToken"):
            raise RuntimeError("登录后未拿到 accessToken")
        logger.info("[补2FA] 登录成功")

        # ---- 2) reauth + enroll + activate（复用注册链路的 setup_2fa）----
        from core.account_export import _activate_totp, _enroll_totp, _trigger_reauth, fetch_session

        session = PageSession(driver)
        reauth_otp_after = time.time()
        auth_url = _trigger_reauth(session, email)
        logger.info("[补2FA] reauth authorize URL 已获取，导航触发验证码")
        driver.get(auth_url)
        time.sleep(3)
        reauth_session = OtpWaitSession(wait_fn=wait_for_otp)
        reauth_code = reauth_session.wait(email, after_ts=reauth_otp_after, max_wait=90)
        logger.info("[补2FA] reauth 验证码已收到（len=%s），提交", len(str(reauth_code or "")))
        _clear_otp_inputs(driver)
        _type_otp(driver, reauth_code)
        _click_continue(driver)
        reauth_outcome = _wait_after_email_otp_submit(driver, timeout=25)
        logger.info("[补2FA] reauth 验证码提交结果：%s", reauth_outcome)
        time.sleep(3)
        fresh_info = _fetch_chatgpt_session(driver, timeout=90)
        fresh_token = str(fresh_info.get("accessToken") or "")
        if not fresh_token:
            raise RuntimeError("reauth 后未拿到新的 accessToken")
        logger.info("[补2FA] reauth 后 token 已刷新")

        session2 = PageSession(driver)
        secret, session_id = _enroll_totp(session2, fresh_token)
        _activate_totp(session2, fresh_token, secret, session_id)
        logger.info("[补2FA] enroll/activate 完成，secret_len=%s", len(str(secret or "")))

        # ---- 3) 复核：重认证后的 token 应带 mfa=totp ----
        mfa_status = ""
        try:
            info2 = fetch_session(session)
            user = (info2 or {}).get("user") or {}
            mfa_status = ",".join(str(x) for x in (user.get("mfa") or []))
            logger.info("[补2FA] 复核 user.mfa=%s", mfa_status or "-")
        except Exception as exc:
            logger.info("[补2FA] 复核失败（不影响结果）：%s", str(exc)[:120])

        if save:
            try:
                from core import db

                acc = db.get_account_by_email(email) or {}
                acc_id = int(acc.get("id") or 0)
                if not acc_id:
                    raise RuntimeError("账号不存在，无法回写 totp_secret")
                # update_account_totp_secret(acc_id, result_dict)：这里必须传账号 id + 结果字典，
                # 之前误传 (email, secret) 会 int(email) 抛错导致 secret 根本没落库。
                db.update_account_totp_secret(
                    acc_id,
                    {"ok": True, "status": "success", "totp_secret": secret, "message": "补设 2FA 完成"},
                )
                logger.info("[补2FA] 已回写 totp_secret 到账号记录")
            except Exception as exc:
                logger.warning("[补2FA] 回写 DB 失败：%s", str(exc)[:160])
        return {"ok": True, "status": "enabled", "email": email, "totp_secret": secret, "mfa": mfa_status}
    except Exception as exc:
        logger.exception("[补2FA] 失败")
        return {"ok": False, "status": "failed", "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        try:
            _watchdog_timer.cancel()
        except Exception:
            pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
