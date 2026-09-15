# -*- coding: utf-8 -*-
"""给无密码（passwordless）账号补设密码。

OpenAI 对无密码账号没有"直接设置密码"的接口，只能走重置流程：
    登录邮箱（authorize/continue）→ POST /passwordless/send-otp → 邮箱 OTP
    → POST /email-otp/validate → POST /password/reset {"password": ...}

关键请求都走页内 fetch（页面 SentinelSDK 自动附带 sentinel token），
否则 Cloudflare 直接 403 "Just a moment..."。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def set_account_password(
    email: str,
    *,
    password: str | None = None,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
    save: bool = True,
) -> dict:
    """给账号补设密码。

    返回 {ok, status, email, password?, error?}
    status: updated / failed / skipped
    """
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "status": "skipped", "error": "email 为空"}

    from core.roxy_registration import _registration_password

    new_password = str(password or "").strip() or _registration_password()

    from core.cloakbrowser_driver import build_cloak_driver
    from core.page_session import PageSession
    from core.chatgpt_auth import signin_openai
    from core.codex_oauth import _post_json
    from core.email_provider import wait_for_otp

    driver = None
    try:
        driver, _opened = build_cloak_driver(proxy=proxy, proxy_selection=proxy_selection)
        session = PageSession(driver)

        driver.get("https://chatgpt.com/auth/login")
        time.sleep(3)
        csrf_resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
        )
        csrf = str((csrf_resp.json() or {}).get("csrfToken") or "")
        if not csrf:
            raise RuntimeError("未取得 csrfToken")
        auth_url = signin_openai(session, csrf, email, prompt="login")
        driver.get(auth_url)
        time.sleep(4)

        t0 = time.time()
        r1 = _post_json(
            session,
            "https://auth.openai.com/api/accounts/authorize/continue",
            {"username": {"kind": "email", "value": email}},
            referer="https://auth.openai.com/log-in",
        )
        if r1.status_code != 200:
            return {"ok": False, "status": "failed", "email": email,
                    "error": f"submit_email HTTP {r1.status_code}: {r1.text[:150]}"}
        page_type = str((r1.json().get("page") or {}).get("type") or "")
        if page_type == "login_password":
            # 服务端已有密码：本函数走重置流程，仍然可以覆盖成新密码。
            logger.info("[补设密码] 账号已有密码，走重置流程：%s", email)
        elif page_type not in {"email_otp_verification"}:
            return {"ok": False, "status": "failed", "email": email, "page_type": page_type,
                    "error": f"非预期的登录页类型：{page_type or 'unknown'}"}

        r2 = _post_json(
            session,
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            {},
            referer="https://auth.openai.com/log-in",
        )
        if r2.status_code != 200:
            return {"ok": False, "status": "failed", "email": email,
                    "error": f"passwordless/send-otp HTTP {r2.status_code}: {r2.text[:150]}"}

        code = wait_for_otp(email, after_ts=t0)
        r3 = _post_json(
            session,
            "https://auth.openai.com/api/accounts/email-otp/validate",
            {"code": code},
            referer="https://auth.openai.com/email-verification",
        )
        if r3.status_code != 200:
            return {"ok": False, "status": "failed", "email": email,
                    "error": f"email-otp/validate HTTP {r3.status_code}: {r3.text[:150]}"}
        page3 = str((r3.json().get("page") or {}).get("type") or "")

        r4 = _post_json(
            session,
            "https://auth.openai.com/api/accounts/password/reset",
            {"password": new_password},
            referer="https://auth.openai.com/reset-password",
        )
        if r4.status_code != 200:
            return {"ok": False, "status": "failed", "email": email, "page_type": page3,
                    "error": f"password/reset HTTP {r4.status_code}: {r4.text[:180]}"}

        if save:
            from core import db
            db.update_account_registration_password(email, new_password)
        logger.info("[补设密码] 成功：%s", email)
        return {"ok": True, "status": "updated", "email": email, "password": new_password,
                "page_type": page3}
    except Exception as exc:
        logger.warning("[补设密码] 失败: %s: %s: %s", email, type(exc).__name__, str(exc)[:180])
        return {"ok": False, "status": "failed", "email": email,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass