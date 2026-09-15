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


def _read_auth_page(driver, timeout: int = 12) -> dict:
    """等登录/验证码页出现输入框，返回 {url, text, inputs}（用于判断是否已过邮箱步）。"""
    end = time.time() + timeout
    state: dict = {}
    while time.time() < end:
        try:
            state = driver.execute_script(r"""
            const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            return {
              url: location.href,
              text: (document.body ? document.body.innerText : '').slice(0, 400),
              inputs: [...document.querySelectorAll('input')].filter(vis).map(el => ({
                type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
                placeholder: el.getAttribute('placeholder') || ''
              }))
            };
            """) or {}
        except Exception:
            state = {}
        inputs = state.get("inputs") or []
        if any(str(i.get("name") or "") in {"code", "one-time-code"} for i in inputs) or \
                any(str(i.get("type") or "").lower() == "email" for i in inputs):
            return state
        time.sleep(0.5)
    return state


def _login_and_fetch_session(driver, session, email: str) -> dict:
    """邮箱 OTP 登录一次并取回 session（含 accessToken/user/account）。

    账号已经创建成功，只是 create_account 返回的回调偶发
    ``?error=invalid_request``（缺参数）导致 chatgpt.com 登录态没建立。
    """
    from core.chatgpt_auth import signin_openai
    from core.codex_oauth import _post_json
    from core.email_provider import wait_for_otp
    from core.account_export import fetch_session

    driver.get("https://chatgpt.com/auth/login")
    time.sleep(3)
    csrf = str((session.get(
        "https://chatgpt.com/api/auth/csrf",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
    ).json() or {}).get("csrfToken") or "")
    if not csrf:
        raise RuntimeError("兜底登录未取得 csrfToken")
    t0 = time.time()
    auth_url = signin_openai(session, csrf, email, prompt="login")
    driver.get(auth_url)
    time.sleep(4)
    code = wait_for_otp(email, after_ts=t0)
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/email-otp/validate",
        {"code": code},
        referer="https://auth.openai.com/email-verification",
    )
    if resp.status_code != 200:
        raise RuntimeError(f"兜底登录 OTP 验证失败 HTTP {resp.status_code}: {resp.text[:150]}")
    data = resp.json() or {}
    cont = str(data.get("continue_url") or (data.get("page") or {}).get("continue_url") or "")
    if cont:
        driver.get(cont if cont.startswith("http") else "https://auth.openai.com" + cont)
        time.sleep(3)
    info = fetch_session(session) or {}
    if not str(info.get("accessToken") or ""):
        raise RuntimeError("兜底登录后仍未拿到 accessToken")
    return info


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
        from core.cloakbrowser_driver import account_fingerprint_seed

        profile_seed = account_fingerprint_seed(email)
        driver, _opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=profile_seed,
        )
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
        #    注意：authorize URL 带 login_hint 时服务端可能已经走到验证码页（新邮箱=注册、
        #    老邮箱=登录）。此时再提交一次邮箱会把新邮箱推进 password 分支并返回
        #    login_password，被误判成"已注册"——所以先看页面，已经在验证码页就跳过提交。
        t0 = time.time()
        state = _read_auth_page(driver)
        on_code_page = any(
            str(i.get("name") or "") in {"code", "one-time-code"} for i in (state.get("inputs") or [])
        )
        registration_password = ""
        if on_code_page:
            logger.info("[协议注册] 已直接落在验证码页，跳过邮箱提交：%s", state.get("url"))
            ptype = "email_otp_verification"
        else:
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
        if ptype == "create_account_password":
            # 新版"密码注册"分支：先提交密码，再走邮箱 OTP。
            # 端点/字段与 openai_auth 里保留的备用实现一致（user/register + email-otp/send）。
            from core.roxy_registration import _registration_password

            registration_password = _registration_password()
            r_pw = _post_json(
                session,
                "https://auth.openai.com/api/accounts/user/register",
                {"username": email, "password": registration_password},
                referer="https://auth.openai.com/create-account/password",
            )
            if r_pw.status_code != 200:
                return {"ok": False, "status": "failed", "email": email,
                        "error": f"user/register HTTP {r_pw.status_code}: {r_pw.text[:150]}"}
            d_pw = r_pw.json() or {}
            page_pw = d_pw.get("page") or {}
            if str(page_pw.get("type") or "") in {"email_otp_send", "email_otp_send_registration"} or \
                    "email-otp/send" in str(d_pw.get("continue_url") or ""):
                session.get(
                    "https://auth.openai.com/api/accounts/email-otp/send",
                    headers=session.get_auth_navigate_headers(
                        referer="https://auth.openai.com/create-account/password"
                    ),
                    allow_redirects=True,
                )
            logger.info("[协议注册] 密码注册分支：密码已提交（len=%s），转入邮箱验证", len(registration_password))
            ptype = "email_otp_verification"
        elif ptype != "email_otp_verification":
            return {"ok": False, "status": "not_fresh", "email": email, "page_type": ptype,
                    "error": "邮箱已被注册或流程非注册（page.type=%s）" % (ptype or "unknown")}

        # 4) 邮箱 OTP
        # iCloud 投递偶发延迟 2~4 分钟（实测 id=220/10:37 那批），单次 wait 超时后
        # 主动请求重发并再等一轮，最多 3 轮；仍失败才算失败。
        from core.openai_auth import send_email_otp

        otp_after_ts = t0
        code = ""
        last_otp_error: Exception | None = None
        for otp_attempt in range(1, 4):
            try:
                code = wait_for_otp(email, after_ts=otp_after_ts)
                break
            except Exception as exc:
                last_otp_error = exc
                logger.warning("[协议注册] 等待 OTP 超时（%s/3）：%s", otp_attempt, str(exc)[:120])
                if otp_attempt >= 3:
                    break
                otp_after_ts = time.time()
                try:
                    send_email_otp(session, referer="https://auth.openai.com/email-verification")
                    logger.info("[协议注册] 已请求重发邮箱验证码，继续等待")
                except Exception as send_exc:
                    logger.warning("[协议注册] 重发验证码请求失败：%s", str(send_exc)[:120])
        if not code:
            return {"ok": False, "status": "failed", "email": email,
                    "error": f"OTP 等待超时: {last_otp_error}"}
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
            try:
                navigate_about_you(session, cont or None)
            except Exception as exc:
                # Cloudflare 偶发挑战（页内 GET 403 "Just a moment"）：真实导航一次建立 clearance 再重试。
                if "403" not in str(exc) and "Just a moment" not in str(exc):
                    raise
                logger.warning("[协议注册] about-you 导航被 Cloudflare 拦截，改真实导航后重试")
                about_url = cont if str(cont or "").startswith("http") else "https://auth.openai.com/about-you"
                driver.get(about_url)
                time.sleep(2)
                navigate_about_you(session, cont or None)
            # create_account 的 sentinel 流名与登录不同：流名不匹配时服务端虽然返回 200，
            # 但回调会变成 ?error=invalid_request（missing required parameter），拿不到 code。
            prev_flow = getattr(session, "_flow", None)
            if prev_flow is not None:
                session._flow = "oauth_create_account"
            try:
                created = create_account(session, name, birthday, None, None)
            finally:
                if prev_flow is not None:
                    session._flow = prev_flow
            created_page = (created or {}).get("page") if isinstance(created, dict) else {}
            created_cont = str(
                (created or {}).get("continue_url")
                or (created_page or {}).get("continue_url")
                or ""
            ) if isinstance(created, dict) else ""
            if created_cont:
                # 回调 URL 必须真实导航一次：它负责把 auth 会话换成 chatgpt.com 的 session cookie，
                # 否则后面 /api/auth/session 只会返回 WARNING_BANNER（没有 accessToken）。
                cont = created_cont
                page_url = str(((created_page or {}).get("payload") or {}).get("url") or "")
                logger.info("[协议注册] 已拿到回调地址（page=%s），导航建立 chatgpt.com 登录态：%s",
                            str((created_page or {}).get("type") or "-"),
                            (page_url or cont)[:120])

        # 6) 拿 session（AT）
        # create_account 之后必须先真实导航回 chatgpt.com 域：auth.openai.com 页面里
        # 页内 fetch https://chatgpt.com/api/auth/session 会因跨域直接 "Failed to fetch"。
        back_url = str(cont or "").strip()
        if back_url and not back_url.startswith("http"):
            back_url = "https://auth.openai.com" + back_url
        try:
            driver.get(back_url or "https://chatgpt.com/")
        except Exception:
            try:
                driver.get("https://chatgpt.com/")
            except Exception:
                pass
        time.sleep(2)
        info: dict = {}
        at = ""
        try:
            info = fetch_session(session) or {}
            at = str(info.get("accessToken") or "")
        except Exception as exc:
            message = str(exc)
            if "Failed to fetch" in message:
                # 页内 fetch 跨域失败：真实导航回 chatgpt.com 再取一次
                try:
                    driver.get("https://chatgpt.com/")
                    time.sleep(2)
                    info = fetch_session(session) or {}
                    at = str(info.get("accessToken") or "")
                except Exception:
                    at = ""
            elif "未拿到 accessToken" not in message:
                raise
        if not at:
            # 账号已创建成功，只是回调没建立 chatgpt.com 登录态 → 用邮箱 OTP 兜底登录取 AT。
            logger.warning("[协议注册] 回调未建立登录态，改用邮箱 OTP 登录取 AT：%s", email)
            recovered = _login_and_fetch_session(driver, session, email)
            at = str(recovered.get("accessToken") or "")
            info = dict(recovered)
            info["recovered_via_login"] = True

        totp_secret = ""
        try:
            from config import twofa as _twofa_cfg

            if bool(getattr(_twofa_cfg, "ENABLE_2FA", False)):
                from core.account_export import setup_2fa

                totp_secret = str(setup_2fa(session, email) or "")
                logger.info("[协议注册] 2FA 已启用，secret_len=%s", len(totp_secret))
        except Exception as exc:
            logger.warning("[协议注册] 2FA 设置失败（账号已注册成功，仅缺少 2FA）：%s", str(exc)[:160])

        row_id = None
        if save:
            extra = {"user": info.get("user"), "account": info.get("account"),
                     "expires": info.get("expires"), "protocol_registration": True,
                     "name": name, "birthday": birthday,
                     "cloak_profile_seed": profile_seed}
            if registration_password:
                extra["registration_password"] = registration_password
            exit_country = str((proxy_selection or {}).get("exit_country") or "").strip().upper()
            if not exit_country:
                from config import proxy as _proxy_cfg

                exit_country = _proxy_cfg.node_country_code(str((proxy_selection or {}).get("node_name") or ""))
            if exit_country:
                extra["proxy_exit_country"] = exit_country
            row_id = save_account_data(
                email=email, access_token=at, totp_secret=totp_secret or None,
                extra=extra,
                email_source="icloud",
                proxy_used=(proxy_selection or {}).get("node_name") or (proxy or ""),
            )
        logger.info("[协议注册] 成功: %s row_id=%s", email, row_id)
        return {"ok": True, "status": "registered", "email": email, "row_id": row_id,
                "access_token": at, "plan": (info.get("account") or {}).get("planType"),
                "page_type": page2.get("type"), "name": name, "birthday": birthday,
                "registration_password": registration_password or None,
                "totp_secret": totp_secret or None}
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
