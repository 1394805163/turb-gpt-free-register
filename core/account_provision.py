# -*- coding: utf-8 -*-
"""一次浏览器会话内完成「补密码 + 补 2FA + 写回 AT」。

背景：CloakBrowser 免费档只有 1 个并发席位，且席位释放有服务端延迟；
补密码、补 2FA 各自开一次浏览器会白白多等一轮席位，还会多消耗一次邮箱验证码。
这里把两件事合并到同一次登录会话里，注册后置流程也可以直接复用。

返回：
    {
      "ok": bool,
      "email": str,
      "password_status": "ok"/"existing"/"failed"/"skipped",
      "twofa_status": "ok"/"existing"/"failed"/"skipped",
      "password": str,        # 成功时
      "totp_secret": str,     # 成功时
      "access_token_written": bool,
      "error": str,           # 失败时
    }
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def _db_has(email_lower: str, field: str) -> bool:
    from core import db

    acc = db.get_account_by_email(email_lower) or {}
    return bool(str(acc.get(field) or "").strip())


def provision_account(
    email: str,
    *,
    do_password: bool = True,
    do_2fa: bool = True,
    password: str | None = None,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
    save: bool = True,
) -> dict:
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "email": "", "error": "email 为空"}

    from core import db
    from core.account_password import (
        EMAIL_VERIFY_MARKER,
        NEW_PASSWORD_MARKER,
        SETTINGS_URL,
        _click_password_add,
        _click_submit,
        _fill_password_inputs,
        _resolve_fresh_account_route,
        _start_driver_watchdog,
        _wait_url_contains,
        _has_code_input,
    )
    from core.cloakbrowser_driver import account_fingerprint_seed, build_cloak_driver
    from core.email_provider import OtpWaitSession, wait_for_otp
    from core.pipeline_concurrency import pipeline_slot
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _fetch_chatgpt_session,
        _fill_password_page_if_present,
        _is_email_verification_page,
        _is_login_password_page,
        _is_mfa_challenge_page,
        _maybe_accept,
        _pass_mfa_challenge_if_needed,
        _submit_email_and_wait_next,
        _type_otp,
        _wait_after_email_otp_submit,
    )
    from core.account_2fa import _stored_password, _submit_login_password

    acc = db.get_account_by_email(email) or {}
    acc_id = int(acc.get("id") or 0)
    need_password = bool(do_password) and not str(acc.get("password") or "").strip()
    need_2fa = bool(do_2fa) and not str(acc.get("totp_secret") or "").strip()
    out: dict = {
        "ok": False,
        "email": email,
        "password_status": "skipped" if not need_password else "pending",
        "twofa_status": "skipped" if not need_2fa else "pending",
        "access_token_written": False,
    }
    if not need_password and not need_2fa:
        out.update({"ok": True, "password_status": "existing", "twofa_status": "existing"})
        return out

    from core.roxy_registration import _registration_password

    new_password = str(password or "").strip() or _registration_password()

    if not proxy and not proxy_selection:
        proxy, proxy_selection = _resolve_fresh_account_route(email)

    _gate = pipeline_slot("live_check")
    _gate.__enter__()
    def _submit_email_code(email: str, after_ts: float, label: str) -> bool:
        """取码 → 填写 → 提交；被服务端拒绝时改用"最新一封"重试一次。

        2026-09-24 实测：同一别名 30 秒内可能连收两封码（11:16:26 / 11:16:54），
        旧码被新码顶掉后提交会被拒，页面停在 email-verification 不动 ——
        收口流程原来忽略提交结果，于是白等 90 秒后拿到 WARNING_BANNER。
        """
        code = OtpWaitSession(wait_fn=wait_for_otp).wait(email, after_ts=after_ts, max_wait=90)
        if not code:
            raise RuntimeError(f"{label} 未取到验证码")
        logger.info("[收口] %s 验证码已收到（len=%s），提交", label, len(str(code)))
        for attempt in (1, 2):
            _clear_otp_inputs(driver)
            _type_otp(driver, code)
            _click_continue(driver)
            outcome = _wait_after_email_otp_submit(driver, timeout=25)
            logger.info("[收口][诊断] %s 验证码提交结果：%s", label, outcome)
            if outcome == "accepted":
                return True
            if attempt == 2:
                return False
            logger.warning("[收口] %s 验证码未被接受（%s），改用最新一封码重试一次", label, outcome)
            time.sleep(3)
            fresh = OtpWaitSession(wait_fn=wait_for_otp).wait(email, after_ts=time.time() - 60, max_wait=60)
            if fresh and str(fresh) != str(code):
                code = fresh
                logger.info("[收口] %s 已换用新码重试", label)
                continue
            return False
        return False

    def _diag(tag: str) -> None:
        """定位"卡在哪一步"用：记录当前页面 URL/标题（只写日志，不改行为）。"""
        try:
            url = str(getattr(driver, "current_url", "") or "")
            title = str(getattr(driver, "title", "") or "")[:60]
            logger.info("[收口][诊断] %s | url=%s | title=%s", tag, url[:160], title)
        except Exception:
            pass

    driver = None
    try:
        driver, opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=account_fingerprint_seed(email),
        )
        driver.set_page_load_timeout(90)
        logger.info("[收口] 启动：%s profile=%s", email, opened.profile_id)
        watchdog = _start_driver_watchdog(driver)

        # ---- 1) 登录（邮箱 OTP）----
        driver.get("https://chatgpt.com/auth/login")
        _maybe_accept(driver)
        time.sleep(2)
        login_otp_after = time.time()
        # 这些号已经补过密码：邮箱提交后 OpenAI 可能直接进「登录密码页」，
        # 必须先过密码页（优先切一次性验证码入口，其次用 DB 里的密码），
        # 否则会话永远拿不到 accessToken（2026-09-24 收口工具卡在
        # 「等待 /api/auth/session accessToken 超时」的根因）。
        next_state = _submit_email_and_wait_next(
            driver, email, attempts=2, timeout=120, allow_password_page=True
        )
        logger.info("[收口][诊断] next_state=%s", next_state)
        _diag("提交邮箱后")
        if next_state == "login_password":
            logger.info("[收口] 进入登录密码页：优先切换一次性验证码入口")
            _fill_password_page_if_present(driver, email, timeout=45)
            if _is_login_password_page(driver):
                stored_password = _stored_password(email)
                if not stored_password:
                    raise RuntimeError("账号已设密码且无一次性验证码入口，DB 中也缺少密码")
                logger.info("[收口] 未找到一次性验证码入口，改用 DB 密码登录")
                _submit_login_password(driver, stored_password)
                time.sleep(4)
                _diag("提交密码后")
        if _is_email_verification_page(driver):
            ok_submit = _submit_email_code(email, login_otp_after, "登录")
            _diag("提交登录验证码后")
            if not ok_submit:
                # 实测：判定 'invalid' 之后页面仍可能已经跳到 chatgpt.com（登录其实成功了），
                # 所以这里不判死，交给下面的 session 读取决定真实状态。
                logger.warning("[收口] 登录验证码提交结果非 accepted，继续读 session 判定真实状态")
        if _is_mfa_challenge_page(driver):
            if not _pass_mfa_challenge_if_needed(driver, email, timeout=30):
                raise RuntimeError("2FA 动态码未通过，无法继续")
            time.sleep(2)
        info = _fetch_chatgpt_session(driver, timeout=90)
        if not (info or {}).get("accessToken"):
            raise RuntimeError("登录后未拿到 accessToken")
        logger.info("[收口] 登录成功，开始处理密码/2FA")

        # ---- 2) 补密码 ----
        if need_password:
            driver.get(SETTINGS_URL)
            time.sleep(6)
            click_ts = time.time()
            clicked = _click_password_add(driver)
            logger.info("[收口][补密码] 点击 Password Add：%s", clicked)
            if not clicked.get("found"):
                raise RuntimeError("设置页未找到 password-setting")
            if _wait_url_contains(driver, EMAIL_VERIFY_MARKER, timeout=45) or _has_code_input(driver):
                add_session = OtpWaitSession(wait_fn=wait_for_otp)
                add_code = add_session.wait(email, after_ts=click_ts, max_wait=90)
                logger.info("[收口][补密码] 验证码已收到（len=%s）", len(str(add_code or "")))
                _clear_otp_inputs(driver)
                _type_otp(driver, add_code)
                _click_continue(driver)
                _wait_after_email_otp_submit(driver, timeout=25)
            if _is_mfa_challenge_page(driver):
                _pass_mfa_challenge_if_needed(driver, email, timeout=30)
                time.sleep(2)
            _wait_url_contains(driver, NEW_PASSWORD_MARKER, timeout=30)
            filled = _fill_password_inputs(driver, new_password)
            if not filled.get("ok"):
                raise RuntimeError(f"新密码页未找到密码输入框：{filled}")
            time.sleep(1.0)
            submit = _click_submit(driver)
            if not submit.get("ok"):
                raise RuntimeError(f"新密码页未找到提交按钮：{submit}")
            left_page = False
            for _ in range(10):
                time.sleep(2)
                if NEW_PASSWORD_MARKER not in str(getattr(driver, "current_url", "") or ""):
                    left_page = True
                    break
            if not left_page:
                raise RuntimeError("提交后仍停留在新密码页")
            if save:
                db.update_account_registration_password(email, new_password)
            out["password_status"] = "ok"
            out["password"] = new_password
            logger.info("[收口][补密码] 完成并回写")

        # ---- 3) 补 2FA（同一会话内 reauth → enroll → activate）----
        if need_2fa:
            from core.account_export import _activate_totp, _enroll_totp, _trigger_reauth
            from core.page_session import PageSession

            session = PageSession(driver)
            reauth_otp_after = time.time()
            auth_url = _trigger_reauth(session, email)
            driver.get(auth_url)
            time.sleep(3)
            ok_reauth = _submit_email_code(email, reauth_otp_after, "补2FA-reauth")
            if not ok_reauth:
                logger.warning("[收口] reauth 验证码提交结果非 accepted，继续读 session 判定真实状态")
            time.sleep(3)
            fresh = _fetch_chatgpt_session(driver, timeout=90)
            fresh_token = str((fresh or {}).get("accessToken") or "")
            if not fresh_token:
                raise RuntimeError("reauth 后未拿到新的 accessToken")
            session2 = PageSession(driver)
            secret, session_id = _enroll_totp(session2, fresh_token)
            _activate_totp(session2, fresh_token, secret, session_id)
            logger.info("[收口][补2FA] enroll/activate 完成，secret_len=%s", len(str(secret or "")))
            if save and acc_id:
                db.update_account_totp_secret(
                    acc_id,
                    {"ok": True, "status": "success", "totp_secret": secret, "message": "组合流程补设 2FA"},
                )
            out["twofa_status"] = "ok"
            out["totp_secret"] = secret

        # ---- 4) 写回最新会话 AT ----
        try:
            fresh = _fetch_chatgpt_session(driver, timeout=60)
            if save and db.update_account_session_tokens(email, fresh):
                out["access_token_written"] = True
        except Exception as exc:
            logger.warning("[收口] 会话 AT 写回失败：%s", str(exc)[:140])

        out["ok"] = out["password_status"] in ("ok", "existing", "skipped") and out["twofa_status"] in ("ok", "existing", "skipped")
        return out
    except Exception as exc:
        from core.cloakbrowser_driver import is_license_busy_error

        if is_license_busy_error(exc):
            out["status"] = "license_busy"
            out["error"] = str(exc)[:200]
            logger.warning("[收口] CloakBrowser 席位居满，稍后重试：%s", str(exc)[:120])
            return out
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.exception("[收口] 失败: %s", email)
        return out
    finally:
        try:
            watchdog.cancel()
        except Exception:
            pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        try:
            _gate.__exit__(None, None, None)
        except Exception:
            pass
