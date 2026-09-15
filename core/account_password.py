# -*- coding: utf-8 -*-
"""给已注册账号补设密码（ChatGPT 设置页 "Password Add" 流程，2026-09 实测）。

流程：
    1. Cloak 浏览器 OTP 登录账号（拿登录态）
    2. 打开设置页 Security 标签，点击 [data-testid="password-setting"]（"Password Add"）
    3. 服务端跳转 auth.openai.com/email-verification，并发一封新的邮箱验证码
    4. 提交验证码 → 进入 reset-password/new-password（设置新密码页）
    5. 填写新密码并提交；成功后回写 DB（update_account_registration_password）

背景：无密码账号没有"未登录补密码"路径（旧实现已废弃），设置页入口是当前唯一
可行通道；它自带 OTP 重认证，与 2FA enroll 的 reauth 前置一致。
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

SETTINGS_URL = "https://chatgpt.com/#settings/Security"
EMAIL_VERIFY_MARKER = "email-verification"
NEW_PASSWORD_MARKER = "new-password"

_PAGE_STATE_JS = r"""
const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
const inputs = [...document.querySelectorAll('input')].filter(vis).map(el => ({
  type: el.type || '', name: el.name || '', id: el.id || '',
  autocomplete: el.getAttribute('autocomplete') || '', placeholder: el.getAttribute('placeholder') || ''
}));
const buttons = [...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(vis).map(el => ({
  text: (el.innerText || el.getAttribute('value') || '').replace(/\s+/g, ' ').trim().slice(0, 60),
  disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
}));
return {url: location.href, title: document.title, inputs, buttons};
"""


def _page_state(driver) -> dict:
    try:
        return driver.execute_script(_PAGE_STATE_JS) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _wait_url_contains(driver, marker: str, timeout: float) -> str:
    end = time.time() + max(1.0, float(timeout))
    while time.time() < end:
        try:
            url = str(getattr(driver, "current_url", "") or "")
        except Exception:
            url = ""
        if url and marker in url:
            return url
        time.sleep(0.5)
    return ""


def _has_code_input(driver) -> bool:
    try:
        return bool(driver.execute_script(r"""
        const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        return [...document.querySelectorAll('input[name="code"],input[autocomplete="one-time-code"]')].some(vis);
        """))
    except Exception:
        return False


def _click_password_add(driver) -> dict:
    return driver.execute_script(r"""
    const el = document.querySelector('[data-testid="password-setting"]');
    if (!el) return {found: false};
    el.scrollIntoView({block: 'center'});
    el.click();
    return {found: true, text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80)};
    """) or {}


def _fill_password_inputs(driver, password: str) -> dict:
    return driver.execute_script(r"""
    const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const inputs = [...document.querySelectorAll('input[type="password"], input[name*="password" i], input[autocomplete="new-password"]')].filter(vis);
    if (!inputs.length) return {ok: false, reason: 'no-password-input'};
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    for (const el of inputs) {
      el.focus();
      if (setter) setter.call(el, arguments[0]); else el.value = arguments[0];
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.blur();
    }
    return {ok: true, count: inputs.length};
    """, password) or {}


def _click_submit(driver) -> dict:
    return driver.execute_script(r"""
    const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
    const btns = [...document.querySelectorAll('button,input[type="submit"],[role="button"]')].filter(el => vis(el) && enabled(el));
    const hit = btns.find(el => /continue|save|submit|set password|update|继续|保存|确定|提交/i.test((el.innerText || el.getAttribute('value') || '').trim()));
    if (!hit) return {ok: false, candidates: btns.map(el => (el.innerText || '').trim().slice(0, 40)).slice(0, 12)};
    hit.scrollIntoView({block: 'center'});
    hit.click();
    return {ok: true, text: (hit.innerText || hit.getAttribute('value') || '').trim()};
    """) or {}


def _resolve_fresh_account_route(email: str) -> tuple[str | None, dict | None]:
    """新号补密码时固定注册出口国家（避免几分钟内跨国跳变的风控特征）。

    预检偶发超时（3s）不再回落到"任意国家默认选路"——那会选中 JP/US 这类
    与注册地不符的节点；改为显式按注册国家直选（不带预检）。
    """
    hint = "SG"
    try:
        from core import db
        from core.live_check_service import _young_account_country_hint

        acc = db.get_account_by_email(email) or {}
        acc_id = int(acc.get("id") or 0)
        hint = (_young_account_country_hint(acc_id) if acc_id else "") or hint
    except Exception:
        pass
    try:
        from core.live_check_service import _resolve_live_check_route

        route = _resolve_live_check_route(None, country_hint=hint)
        return route.get("proxy"), route.get("proxy_selection")
    except Exception as exc:
        logger.warning("[补密码] 出口预检失败，改按注册国家直选节点：%s", str(exc)[:120])
        try:
            from config import proxy as proxy_cfg

            selection = proxy_cfg.pick_registration_proxy(allowed_countries_override={hint})
            return None, selection
        except Exception as exc2:
            logger.warning("[补密码] 按国家直选也失败，交给默认选路：%s", str(exc2)[:120])
            return None, None


def set_account_password(
    email: str,
    *,
    password: str | None = None,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
    save: bool = True,
) -> dict:
    """给已注册账号补设密码。

    返回 {ok, status, email, password?, error?}
    status: updated / failed / skipped
    """
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "status": "skipped", "error": "email 为空"}

    from core.roxy_registration import _registration_password

    new_password = str(password or "").strip() or _registration_password()

    from core.cloakbrowser_driver import account_fingerprint_seed, build_cloak_driver
    from core.email_provider import OtpWaitSession, wait_for_otp
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _fetch_chatgpt_session,
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
        logger.info("[补密码] 启动：%s profile=%s", email, opened.profile_id)

        # ---- 1) OTP 登录 ----
        driver.get("https://chatgpt.com/auth/login")
        _maybe_accept(driver)
        time.sleep(2)
        login_otp_after = time.time()
        _submit_email_and_wait_next(driver, email, attempts=2, timeout=120)
        login_session = OtpWaitSession(wait_fn=wait_for_otp)
        login_code = login_session.wait(email, after_ts=login_otp_after, max_wait=90)
        logger.info("[补密码] 登录验证码已收到（len=%s），提交", len(str(login_code or "")))
        _clear_otp_inputs(driver)
        _type_otp(driver, login_code)
        _click_continue(driver)
        outcome = _wait_after_email_otp_submit(driver, timeout=25)
        logger.info("[补密码] 登录验证码提交结果：%s", outcome)
        time.sleep(2)
        info = _fetch_chatgpt_session(driver, timeout=90)
        if not info.get("accessToken"):
            raise RuntimeError("登录后未拿到 accessToken")
        logger.info("[补密码] 登录成功")

        # ---- 2) 设置页 -> Password Add ----
        driver.get(SETTINGS_URL)
        time.sleep(6)
        click_ts = time.time()
        clicked = _click_password_add(driver)
        logger.info("[补密码] 点击 Password Add：%s", clicked)
        if not clicked.get("found"):
            raise RuntimeError(
                f"设置页未找到 password-setting：{json.dumps(_page_state(driver), ensure_ascii=False)[:260]}"
            )

        # ---- 3) 邮箱验证码重认证（新 OTP）----
        # OTP 邮件在点击时即发送；服务端跳转偶发延迟 30s+，等待窗口给足。
        if _wait_url_contains(driver, EMAIL_VERIFY_MARKER, timeout=45) or _has_code_input(driver):
            add_session = OtpWaitSession(wait_fn=wait_for_otp)
            add_code = add_session.wait(email, after_ts=click_ts, max_wait=90)
            logger.info("[补密码] 添加密码验证码已收到（len=%s），提交", len(str(add_code or "")))
            _clear_otp_inputs(driver)
            _type_otp(driver, add_code)
            _click_continue(driver)
            outcome2 = _wait_after_email_otp_submit(driver, timeout=25)
            logger.info("[补密码] 添加密码验证码提交结果：%s", outcome2)
        else:
            logger.info("[补密码] 未进入邮箱验证码页（继续等待新密码页）")

        # ---- 4) 设置新密码页 ----
        _wait_url_contains(driver, NEW_PASSWORD_MARKER, timeout=30)
        state = _page_state(driver)
        logger.info("[补密码] 新密码页快照：%s", json.dumps(state, ensure_ascii=False)[:420])

        filled = _fill_password_inputs(driver, new_password)
        logger.info("[补密码] 填写密码输入框：%s", filled)
        if not filled.get("ok"):
            raise RuntimeError(f"新密码页未找到密码输入框：{filled}")
        time.sleep(1.0)
        submit = _click_submit(driver)
        logger.info("[补密码] 提交密码：%s", submit)
        if not submit.get("ok"):
            raise RuntimeError(f"新密码页未找到提交按钮：{submit}")

        # ---- 5) 结果判定 ----
        left_page = False
        for _ in range(10):
            time.sleep(2)
            url = str(getattr(driver, "current_url", "") or "")
            if NEW_PASSWORD_MARKER not in url:
                left_page = True
                break
        final = _page_state(driver)
        logger.info("[补密码] 提交后页面：%s", json.dumps(final, ensure_ascii=False)[:420])
        final_url = str(final.get("url") or "")
        if not left_page and NEW_PASSWORD_MARKER in final_url:
            return {
                "ok": False, "status": "failed", "email": email,
                "error": f"提交后仍停留在新密码页：{json.dumps(final, ensure_ascii=False)[:240]}",
            }

        # 复核：回到设置页读"密码"行（已设置会从 "Add" 变为 "Change"/"更改"）
        verify_label = ""
        try:
            driver.get(SETTINGS_URL)
            time.sleep(5)
            verify_label = str(driver.execute_script(r"""
            const el = document.querySelector('[data-testid="password-setting"]');
            return el ? (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) : '';
            """) or "")
            logger.info("[补密码] 设置页复核：password-setting=%s", verify_label)
        except Exception as exc:
            logger.info("[补密码] 设置页复核失败（不影响结果）：%s", str(exc)[:120])

        if save:
            try:
                from core import db

                db.update_account_registration_password(email, new_password)
                logger.info("[补密码] 已回写密码到账号记录")
            except Exception as exc:
                logger.warning("[补密码] 回写 DB 失败：%s", str(exc)[:160])
        return {
            "ok": True, "status": "updated", "email": email, "password": new_password,
            "url": final_url, "settings_label": verify_label,
        }
    except Exception as exc:
        logger.exception("[补密码] 失败")
        return {"ok": False, "status": "failed", "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
