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
import re
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


_FIND_PASSWORD_BUTTON_JS = r"""
const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const ACTION = /^(add|change|update|set|remove|添加|更改|设置|移除)$/i;
const CLICKABLE = 'button,[role=button],a';
// 1) 独立按钮：文案本身就是 "Add Password" / "Change password"
for (const b of [...document.querySelectorAll(CLICKABLE)].filter(vis)) {
  const t = norm(b.innerText || b.getAttribute('aria-label') || '');
  if (/^(add|set|change|update)\s*password$/i.test(t)) return b;
}
// 2) 2026-09 版设置页把整行做成一个可点元素，innerText 形如
//    "Password Manage the password you use to log in Add"（没有独立的 Add 按钮）
for (const b of [...document.querySelectorAll(CLICKABLE)].filter(vis)) {
  const t = norm(b.innerText || b.getAttribute('aria-label') || '');
  if (t.length > 160) continue;            // 排除整页/整栏容器
  if (!/^(password|密码)/i.test(t)) continue;
  const inner = [...b.querySelectorAll('button,[role=button]')].filter(vis)
    .find(x => ACTION.test(norm(x.innerText || x.getAttribute('aria-label') || '')));
  return inner || b;
}
// 3) 兜底：文案恰为 Password 的行标签 + 同一行右侧的 Add/Change 按钮
const labels = [...document.querySelectorAll('div,span,h2,h3,h4,p')]
  .filter(el => vis(el) && /^(password|密码)$/i.test(norm(el.innerText || '')))
  .map(el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top + r.height / 2}; })
  .filter(o => o.y > 0 && o.x >= 0);
if (!labels.length) return null;
let best = null, bestScore = 1e9;
for (const b of [...document.querySelectorAll('button,[role=button]')].filter(vis)) {
  const t = norm(b.innerText || b.getAttribute('aria-label') || '');
  if (!ACTION.test(t)) continue;
  const r = b.getBoundingClientRect();
  const cy = r.top + r.height / 2;
  for (const p of labels) {
    if (r.left < p.x) continue;
    const dy = Math.abs(cy - p.y);
    if (dy > 40) continue;
    if (dy < bestScore) { bestScore = dy; best = b; }
  }
}
return best;
"""


def _find_password_button(driver, timeout: float = 90.0):
    """定位 Security 页密码行按钮，返回 (element, mode)。

    2026-09 起 ChatGPT 设置页整页只剩一个 data-testid，老的
    [data-testid="password-setting"] 已不存在；优先按老 testid 快速命中，
    找不到再按文案定位（支持 Add/Change Password 按钮或 Password 行内的按钮）。
    """
    from core.roxy_registration import _find_any

    try:
        el = _find_any(driver, ['[data-testid="password-setting"]'], timeout=5)
        if el is not None:
            return el, "testid"
    except Exception:
        pass

    end = time.time() + max(5.0, float(timeout))
    while time.time() < end:
        try:
            el = driver.execute_script(_FIND_PASSWORD_BUTTON_JS)
        except Exception as exc:
            logger.info("[补密码] 文案定位异常：%s", str(exc)[:140])
            el = None
        if el is not None:
            return el, "text_row"
        time.sleep(1.5)
    return None, ""


def _click_password_add(driver) -> dict:
    """点击 Password Add：优先真实鼠标点击（CDP 坐标点击），失败回退页面内 click()。"""
    from core.roxy_registration import _human_click

    el, mode = _find_password_button(driver, timeout=90)
    if el is None:
        return {"found": False, "mode": mode}
    try:
        text = str(el.text or "").replace("\n", " ").strip()[:80]
    except Exception:
        text = ""
    before = str(getattr(driver, "current_url", "") or "")
    try:
        _human_click(driver, el, label="password_add")
        time.sleep(2.0)
    except Exception as exc:
        logger.info("[补密码] 真实点击 Password Add 失败，回退脚本点击：%s", str(exc)[:140])
        try:
            el.click()
            time.sleep(1.5)
        except Exception:
            return {"found": False, "mode": mode}
        return {"found": True, "text": text, "mode": mode + "+js_fallback"}
    after = str(getattr(driver, "current_url", "") or "")
    if after != before or "password" in after.lower():
        return {"found": True, "text": text, "mode": mode + "+human_click"}
    logger.warning("[补密码] 真实点击 Password Add 后页面未变化，回退脚本点击")
    try:
        el.click()
        time.sleep(1.5)
    except Exception:
        pass
    return {"found": True, "text": text, "mode": mode + "+human_click+js_fallback"}


def _fill_password_inputs(driver, password: str) -> dict:
    """填写新密码：优先逐字符真实键盘输入，失败回退 JS setter。"""
    try:
        from core.roxy_registration import _human_type_text

        handles = driver.find_elements("css selector", 'input[type="password"], input[autocomplete="new-password"]')
        visible = []
        for item in handles:
            try:
                if item.is_displayed():
                    visible.append(item)
            except Exception:
                continue
        if visible:
            for item in visible:
                try:
                    item.clear()
                except Exception:
                    pass
                _human_type_text(driver, item, password, clear=True)
            # 真实键入后校验：React 受控输入偶发没吃到键值，必须回退 JS setter，
            # 否则提交时按钮仍是 disabled，表现为"提交后仍停留在新密码页"。
            try:
                values = [str(it.get_attribute("value") or "") for it in visible]
            except Exception:
                values = []
            if values and all(v == password for v in values):
                return {"ok": True, "count": len(visible), "mode": "human_type"}
            logger.warning("[补密码] 真实键入未生效（values=%s），回退 JS setter", [len(v) for v in values])
    except Exception as exc:
        logger.info("[补密码] 真实键入密码失败，回退 JS setter：%s", str(exc)[:140])
    except Exception as exc:
        logger.info("[补密码] 真实键入密码失败，回退 JS setter：%s", str(exc)[:140])
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
    """提交新密码：优先真实鼠标点击，失败回退页面内 click()。"""
    try:
        from core.roxy_registration import _human_click

        buttons = driver.find_elements("css selector", 'button, input[type="submit"], [role="button"]')
        hit = None
        for item in buttons:
            try:
                if not item.is_displayed() or not item.is_enabled():
                    continue
                label = str(item.text or item.get_attribute("value") or "").strip()
            except Exception:
                continue
            if re.search(r"continue|save|submit|set password|update|继续|保存|确定|提交", label, re.I):
                hit = item
                break
        if hit is not None:
            text = str(hit.text or hit.get_attribute("value") or "").strip()
            marker = NEW_PASSWORD_MARKER
            _human_click(driver, hit, label="password_submit")
            time.sleep(2.5)
            url_now = str(getattr(driver, "current_url", "") or "")
            if marker not in url_now:
                return {"ok": True, "text": text, "mode": "human_click"}
            logger.warning("[补密码] 真实点击提交后仍在密码页，回退脚本点击")
            try:
                hit.click()
                time.sleep(1.5)
            except Exception:
                pass
            return {"ok": True, "text": text, "mode": "human_click+js_fallback"}
    except Exception as exc:
        logger.info("[补密码] 真实点击提交失败，回退脚本点击：%s", str(exc)[:140])
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

            # 账号注册地之外再并入全局允许地区（US/JP/TW/SG 之类）：
            # 后置流程（补密码/补2FA/查活）只要求落在允许地区内，
            # 单一国家选不出节点时不该把整条流程判死。
            allowed_union = {hint} | {
                str(code).strip().upper()
                for code in (getattr(proxy_cfg, "REGISTRATION_PROXY_ALLOWED_COUNTRIES", []) or [])
                if str(code).strip()
            } | {
                str(code).strip().upper()
                for code in (getattr(proxy_cfg, "MIHOMO_REGISTRATION_ALLOWED_COUNTRIES", []) or [])
                if str(code).strip()
            }
            selection = proxy_cfg.pick_registration_proxy(allowed_countries_override=allowed_union)
            return None, selection
        except Exception as exc2:
            logger.warning("[补密码] 按国家直选也失败，交给默认选路：%s", str(exc2)[:120])
            return None, None


def refresh_account_session(
    email: str,
    *,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
    save: bool = True,
) -> dict:
    """只做 OTP 登录并把新会话写回账号（不设置密码）。

    用途：补密码/重新登录会让注册期 AT 被服务端吊销（401 token_revoked），
    重新登录一次即可换回可用会话。返回 {ok, status, email, access_token?, error?}
    """
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "status": "skipped", "error": "email 为空"}

    from core.cloakbrowser_driver import account_fingerprint_seed, build_cloak_driver
    from core.email_provider import OtpWaitSession, wait_for_otp
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _fetch_chatgpt_session,
        _is_mfa_challenge_page,
        _maybe_accept,
        _pass_mfa_challenge_if_needed,
        _submit_email_and_wait_next,
        _type_otp,
        _wait_after_email_otp_submit,
    )

    if not proxy and not proxy_selection:
        proxy, proxy_selection = _resolve_fresh_account_route(email)

    from core.pipeline_concurrency import pipeline_slot

    # 浏览器闸门：与注册/查活共用 CloakBrowser 免费档的唯一并发席位，
    # 避免补密码/补2FA 与注册流水线同时开浏览器触发 session limit。
    _gate = pipeline_slot("live_check")
    _gate.__enter__()
    driver = None
    try:
        driver, opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=account_fingerprint_seed(email),
        )
        driver.set_page_load_timeout(90)
        logger.info("[会话刷新] 启动：%s profile=%s", email, opened.profile_id)
        _watchdog_timer = _start_driver_watchdog(driver)

        driver.get("https://chatgpt.com/auth/login")
        _maybe_accept(driver)
        time.sleep(2)
        login_otp_after = time.time()
        _submit_email_and_wait_next(driver, email, attempts=2, timeout=120)
        login_session = OtpWaitSession(wait_fn=wait_for_otp)
        login_code = login_session.wait(email, after_ts=login_otp_after, max_wait=90)
        logger.info("[会话刷新] 验证码已收到（len=%s），提交", len(str(login_code or "")))
        _clear_otp_inputs(driver)
        _type_otp(driver, login_code)
        _click_continue(driver)
        outcome = _wait_after_email_otp_submit(driver, timeout=25)
        logger.info("[会话刷新] 验证码提交结果：%s", outcome)
        time.sleep(2)
        if _is_mfa_challenge_page(driver):
            if not _pass_mfa_challenge_if_needed(driver, email, timeout=30):
                raise RuntimeError("2FA 动态码未通过")
            time.sleep(2)
        info = _fetch_chatgpt_session(driver, timeout=90)
        new_at = str((info or {}).get("accessToken") or "").strip()
        if not new_at:
            raise RuntimeError("登录后未拿到 accessToken")

        written = False
        if save:
            from core import db

            written = db.update_account_session_tokens(email, info)
            logger.info("[会话刷新] 新会话写回：%s", written)
        return {
            "ok": True, "status": "refreshed", "email": email,
            "access_token": new_at, "written": written,
        }
    except Exception as exc:
        from core.cloakbrowser_driver import is_license_busy_error

        if is_license_busy_error(exc):
            logger.warning("[会话刷新] CloakBrowser 席位居满，稍后重试：%s", str(exc)[:120])
            return {"ok": False, "status": "license_busy", "email": email, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        logger.exception("[会话刷新] 失败")
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
        try:
            _gate.__exit__(None, None, None)
        except Exception:
            pass


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
    # 事故复盘(2026-09-20):旧次序是"设置成功后才写库"，一旦后续步骤失败，
    # 随机密码值就丢失（服务端可能已生效、本地却不知道密码）。改为生成后立即登记：
    # 设置成功是正式值；失败也保留此值，便于人工核验或重试。
    if save:
        try:
            from core import db as _db_register
            _db_register.update_account_registration_password(email, new_password)
            logger.info("[补密码] 密码值已先行登记（防丢失）")
        except Exception as _reg_exc:
            logger.warning("[补密码] 密码值先行登记失败（继续设置）：%s", str(_reg_exc)[:140])

    from core.cloakbrowser_driver import account_fingerprint_seed, build_cloak_driver
    from core.email_provider import OtpWaitSession, wait_for_otp
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _fetch_chatgpt_session,
        _is_mfa_challenge_page,
        _maybe_accept,
        _pass_mfa_challenge_if_needed,
        _submit_email_and_wait_next,
        _type_otp,
        _wait_after_email_otp_submit,
    )

    if not proxy and not proxy_selection:
        proxy, proxy_selection = _resolve_fresh_account_route(email)

    from core.pipeline_concurrency import pipeline_slot

    # 浏览器闸门：与注册/查活共用 CloakBrowser 免费档的唯一并发席位，
    # 避免补密码/补2FA 与注册流水线同时开浏览器触发 session limit。
    _gate = pipeline_slot("live_check")
    _gate.__enter__()
    driver = None
    try:
        driver, opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=account_fingerprint_seed(email),
        )
        driver.set_page_load_timeout(90)
        logger.info("[补密码] 启动：%s profile=%s", email, opened.profile_id)
        _watchdog_timer = _start_driver_watchdog(driver)

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
        # 账号已启用 2FA 时登录后会停在 mfa-challenge，需要本地 TOTP 动态码。
        if _is_mfa_challenge_page(driver):
            logger.info("[补密码] 检测到 2FA 挑战页，提交本地 TOTP 动态码")
            if not _pass_mfa_challenge_if_needed(driver, email, timeout=30):
                raise RuntimeError("2FA 动态码未通过，无法继续设置密码")
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

        # 账号启用 2FA 时，"添加密码"重新认证后会再次要求 TOTP 动态码。
        if _is_mfa_challenge_page(driver):
            logger.info("[补密码] 添加密码步骤检测到 2FA 挑战页，提交本地 TOTP 动态码")
            if not _pass_mfa_challenge_if_needed(driver, email, timeout=30):
                raise RuntimeError("添加密码步骤的 2FA 动态码未通过")
            time.sleep(2)

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
            # 登录 + 设置密码会重新签发会话并吊销注册期 AT（token_revoked）。
            # 不写回新 accessToken，账号会在套餐查询/查活里假死成"AT 已过期"。
            try:
                from core import db as _db

                fresh = _fetch_chatgpt_session(driver, timeout=60)
                if _db.update_account_session_tokens(email, fresh):
                    logger.info("[补密码] 已写回新会话 accessToken（旧注册期 AT 已被服务端吊销）")
                else:
                    logger.warning("[补密码] 新会话写回失败：未读到 accessToken")
            except Exception as exc:
                logger.warning("[补密码] 写回新会话失败：%s", str(exc)[:160])
        return {
            "ok": True, "status": "updated", "email": email, "password": new_password,
            "url": final_url, "settings_label": verify_label,
        }
    except Exception as exc:
        from core.cloakbrowser_driver import is_license_busy_error

        if is_license_busy_error(exc):
            logger.warning("[补密码] CloakBrowser 席位居满，稍后重试：%s", str(exc)[:120])
            return {"ok": False, "status": "license_busy", "email": email, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        logger.exception("[补密码] 失败")
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
        try:
            _gate.__exit__(None, None, None)
        except Exception:
            pass
