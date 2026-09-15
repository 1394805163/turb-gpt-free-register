# -*- coding: utf-8 -*-
"""协议注册：内核浏览器页面会话 + 协议组件（不跑完整 UI 交互）。

与 run_codex_oauth 的 protocol 驱动同思路：真实导航过 Cloudflare，
关键请求走页内 fetch（页面 SDK 自动附带 sentinel token + so），
避免 UI 元素交互（官网元素变动不影响本流程）。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def run_protocol_registration(
    email: str,
    *,
    proxy: str | None = None,
    proxy_selection: dict | None = None,
    name: str = "",
    birthday: str = "",
    save: bool = True,
) -> dict:
    """用页面会话完成一次 ChatGPT 账号注册。

    返回 dict：{ok, status, email, page_type?, access_token?, row_id?, error?}
    - status=registered：成功（可选落盘）
    - status=not_fresh：邮箱已被注册/不可用于注册（page_type=login_password 等）
    - status=failed：流程异常（error 字段含原因）
    """
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "status": "skipped", "error": "email 为空"}

    from core.registration_service import _random_display_name
    from core.profile_utils import generate_random_birthday

    name = name or _random_display_name()
    birthday = birthday or generate_random_birthday()

    from core.cloakbrowser_driver import build_cloak_driver
    from core.page_session import PageSession
    from core.chatgpt_auth import signin_openai
    from core.codex_oauth import _post_json
    from core.openai_auth import navigate_about_you, create_account
    from core.account_export import fetch_session, save_account_data
    from core.email_provider import wait_for_otp

    driver = None
    try:
        driver, _opened = build_cloak_driver(proxy=proxy, proxy_selection=proxy_selection)
        session = PageSession(driver)

        # 1) chatgpt.com 域内取 csrf + signin（页内 fetch）
        driver.get("https://chatgpt.com/auth/login")
        time.sleep(3)
        csrf_resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
        )
        csrf = str((csrf_resp.json() or {}).get("csrfToken") or "")
        if not csrf:
            raise RuntimeError("未取得 csrfToken")
        auth_url = signin_openai(session, csrf, email, prompt="login_or_signup")

        # 2) 真实导航过 Cloudflare（页内 fetch 直连 authorize 会被拦）
        driver.get(auth_url)
        time.sleep(4)

        # 3) 提交邮箱（页内 fetch，sentinel 由页面 SDK 生成）
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
        d1 = r1.json()
        ptype = str((d1.get("page") or {}).get("type") or "")
        if ptype != "email_otp_verification":
            return {"ok": False, "status": "not_fresh", "email": email, "page_type": ptype,
                    "error": "邮箱已被注册或流程非注册（page.type=%s）" % (ptype or "unknown")}

        # 4) 邮箱 OTP
        code = wait_for_otp(email, after_ts=t0)
        r2 = _post_json(
            session,
            "https://auth.openai.com/api/accounts/email-otp/validate",
            {"code": code},
            referer="https://auth.openai.com/email-verification",
        )
        if r2.status_code != 200:
            return {"ok": False, "status": "failed", "email": email,
                    "error": f"OTP 验证失败 HTTP {r2.status_code}: {r2.text[:150]}"}
        d2 = r2.json()
        page2 = d2.get("page") or {}
        cont = str(d2.get("continue_url") or page2.get("continue_url") or "")

        # 5) 资料页（如出现）
        if "about-you" in cont or str(page2.get("type") or "") in {"about_you", "about-you"}:
            navigate_about_you(session, cont or None)
            create_account(session, name, birthday, None, None)

        # 6) 拿 session（AT）
        time.sleep(2)
        info = fetch_session(session)
        at = str(info.get("accessToken") or "")
        if not at:
            return {"ok": False, "status": "failed", "email": email,
                    "error": "注册后未拿到 accessToken", "page_type": page2.get("type"),
                    "continue_url": cont[:120]}

        row_id = None
        if save:
            row_id = save_account_data(
                email=email, access_token=at, totp_secret=None,
                extra={"user": info.get("user"), "account": info.get("account"),
                       "expires": info.get("expires"), "protocol_registration": True,
                       "name": name, "birthday": birthday},
                email_source="icloud",
                proxy_used=(proxy_selection or {}).get("node_name") or (proxy or ""),
            )
        logger.info("[协议注册] 成功: %s row_id=%s", email, row_id)
        return {"ok": True, "status": "registered", "email": email, "row_id": row_id,
                "access_token": at, "plan": (info.get("account") or {}).get("planType"),
                "page_type": page2.get("type"), "name": name, "birthday": birthday}
    except Exception as exc:
        logger.warning("[协议注册] 失败: %s: %s: %s", email, type(exc).__name__, str(exc)[:180])
        return {"ok": False, "status": "failed", "email": email,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
