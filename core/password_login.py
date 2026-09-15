# -*- coding: utf-8 -*-
"""邮箱+密码（+TOTP 2FA）→ ChatGPT AT/RT（纯协议，不开浏览器）。

流程：authorize（Platform Client + PKCE）→ sentinel(flow=password_verify)
→ password/verify →[mfa_challenge 分支：pyotp TOTP]→ authorization code
→ oauth/token → AT/RT/id_token →（可选）CAS 写回注册机 DB。

参考实现（均在产线验证过）：
- yukkcat/chatgpt2api 隐藏历史 5a4435f: services/account_service._login_with_password
- chatgpt2api-proxy-pool-dev 1.8.6 同函数
本地复用：core/openai_auth.py 的 sentinel（Node 真实 sdk.js）与 validate_mfa_totp；
core/codex_oauth.exchange_codex_token（与平台 client_id / redirect_uri 完全一致）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_OAUTH_AUDIENCE = "https://api.openai.com/v1"


def _generate_pkce() -> tuple[str, str]:
    """PKCE：verifier=base64url(64B)，challenge=base64url(S256(verifier))（与下游 utils/pkce.py 同款）。"""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _extract_code(url: str) -> str:
    """从回调 URL 的 query 提取 OAuth authorization code（没有则空串）。"""
    try:
        qs = parse_qs(urlparse(str(url or "")).query)
    except Exception:
        return ""
    return str((qs.get("code") or [""])[0]).strip()


def _parse_error_page(final_url: str) -> tuple[str, dict]:
    """解析 auth.openai.com/error?payload=<base64> 错误页，返回 (error_code, payload)。"""
    try:
        qs = parse_qs(urlparse(final_url).query)
        raw = (qs.get("payload") or [""])[0]
        raw += "=" * ((4 - len(raw) % 4) % 4)
        payload = json.loads(base64.b64decode(raw))
        return str(payload.get("errorCode") or ""), payload
    except Exception as exc:
        return "", {"parse_error": str(exc)[:120]}


def _resp_json(resp) -> dict:
    try:
        return resp.json() if getattr(resp, "text", "") else {}
    except Exception:
        return {}


def _jwt_payload(token: str) -> dict:
    """不验签解 JWT payload。"""
    try:
        seg = str(token or "").split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def _follow_to_code(session, url: str, *, max_hops: int = 8) -> str:
    """逐跳跟随 continue_url，直到拿到带 code 的授权回调地址。"""
    code = _extract_code(url)
    if code:
        return code
    current = str(url or "")
    for hop in range(max_hops):
        if current.startswith("/"):
            current = AUTH_BASE + current
        headers = session.get_auth_navigate_headers(referer=f"{AUTH_BASE}/log-in")
        resp = session.get(current, headers=headers, allow_redirects=False)
        loc = str(resp.headers.get("location") or resp.headers.get("Location") or "")
        logger.info("[password-login] follow hop=%s status=%s loc=%s",
                    hop, getattr(resp, "status_code", ""), loc[:110])
        if not loc:
            raise RuntimeError(
                f"follow 中断（status={getattr(resp, 'status_code', '')} 无 Location）: {current[:120]}")
        current = loc if loc.startswith("http") else AUTH_BASE + loc
        code = _extract_code(current)
        if code:
            return code
    raise RuntimeError(f"跟随 continue_url 超过 {max_hops} 跳仍未取得 code: {current[:120]}")


def login_with_password(
    email: str,
    password: str,
    *,
    totp_secret: str = "",
    proxy: str | None = None,
    country_hint: str = "",
    write_back: bool = False,
    timeout: int = 30,
) -> dict:
    """邮箱+密码登录，返回 {ok, access_token, refresh_token, id_token, ...}。

    Args:
        totp_secret: 账号 2FA 密钥（base32）；账号启用 2FA 时必填。
        proxy: 显式代理；None 时按 country_hint 解析或走默认池。
        country_hint: 出口国家（如 "SG"/"US"），用于锁定与账号一致的登录出口。
        write_back: True 时经 CAS 写回注册机 DB（update_account_chatgpt_oauth）。
    """
    target = str(email or "").strip()
    if not target or not password:
        return {"ok": False, "error": "email/password 为空"}

    step = []

    def _mark(name: str, ok: bool, detail: str = "") -> None:
        step.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        logger.info("[password-login] %s ok=%s %s", name, ok, str(detail)[:160])

    resolved_proxy = proxy
    if resolved_proxy is None and country_hint:
        try:
            from core.live_check_service import _resolve_live_check_route
            route = _resolve_live_check_route(None, country_hint=country_hint)
            resolved_proxy = route.get("proxy")
        except Exception as exc:
            logger.warning("[password-login] 出口解析失败，回退默认池: %s", str(exc)[:160])

    from core.session import BrowserSession
    from config.codex import CODEX_AUTH0_CLIENT

    session = BrowserSession(proxy=resolved_proxy)
    t0 = time.time()
    try:
        code_verifier, code_challenge = _generate_pkce()
        device_id = session.device_id

        # ① OAuth authorize（Platform Client + PKCE）
        params = {
            "issuer": AUTH_BASE,
            "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
            "audience": PLATFORM_OAUTH_AUDIENCE,
            "redirect_uri": f"{PLATFORM_BASE}/auth/callback",
            "device_id": device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": target,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": CODEX_AUTH0_CLIENT,
        }
        authorize_url = f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
        headers = session.get_auth_navigate_headers(referer=f"{PLATFORM_BASE}/")
        headers["sec-fetch-site"] = "cross-site"
        resp = session.get(authorize_url, headers=headers, allow_redirects=True)
        final_url = str(getattr(resp, "url", "") or "")
        if getattr(resp, "status_code", 0) not in (200, 302):
            _mark("authorize", False, f"status={resp.status_code}")
            return {"ok": False, "error": f"authorize_failed_{resp.status_code}",
                    "detail": {"url": final_url, "text": (resp.text or "")[:300]}, "steps": step}
        if "/error" in final_url and "payload=" in final_url:
            error_code, detail = _parse_error_page(final_url)
            _mark("authorize", False, error_code or "error_page")
            return {"ok": False, "error": error_code or "authorize_error", "detail": detail, "steps": step}
        _mark("authorize", True, final_url[:120])

        # ② sentinel token（best-effort：失败也继续，与下游 try/except 行为一致）
        sentinel_ok = False
        token_header = so_header = None
        try:
            from core.openai_auth import build_sentinel_header, request_sentinel_token
            sent = request_sentinel_token(session, "password_verify")
            token_header, so_header = build_sentinel_header(session, sent, "password_verify")
            sentinel_ok = bool(token_header)
        except Exception as exc:
            logger.warning("[password-login] sentinel 获取失败（继续尝试）: %s", str(exc)[:200])
        _mark("sentinel", sentinel_ok)

        # ③ 提交密码
        login_headers = session.get_auth_headers(referer=f"{AUTH_BASE}/log-in")
        login_headers["oai-device-id"] = device_id
        if sentinel_ok:
            login_headers["openai-sentinel-token"] = token_header
            if so_header:
                login_headers["openai-sentinel-so-token"] = so_header
        login_resp = session.post(
            f"{AUTH_BASE}/api/accounts/password/verify",
            headers=login_headers,
            data=json.dumps({"password": password}),
            timeout=timeout,
        )
        login_data = _resp_json(login_resp)
        if login_resp.status_code != 200:
            error_obj = login_data.get("error") if isinstance(login_data.get("error"), dict) else {}
            error_code = str(error_obj.get("code") or "")
            error_msg = str(error_obj.get("message") or "")
            if error_code == "unsupported_country_region_territory":
                _mark("password_verify", False, error_code)
                return {"ok": False, "error": error_code, "detail": login_data, "steps": step}
            if "Invalid credentials" in error_msg or "wrong password" in error_msg.lower():
                _mark("password_verify", False, "invalid_password")
                return {"ok": False, "error": "invalid_password", "detail": login_data, "steps": step}
            _mark("password_verify", False, f"status={login_resp.status_code} code={error_code}")
            return {"ok": False, "error": f"password_verify_failed_{login_resp.status_code}",
                    "detail": login_data, "steps": step}
        _mark("password_verify", True)

        # ④ 取 authorization code（直出 / MFA / 邮箱 OTP）
        code = _extract_code(str(login_data.get("continue_url") or ""))
        page = login_data.get("page") if isinstance(login_data.get("page"), dict) else {}
        page_type = str(page.get("type") or "")

        if not code and page_type == "mfa_challenge":
            payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
            factors = payload.get("factors") or []
            factor_id = str((factors[0] or {}).get("id") or "") if factors else ""
            if not totp_secret:
                _mark("mfa_challenge", False, "缺少 totp_secret")
                return {"ok": False, "error": "need_totp_secret", "detail": login_data, "steps": step}
            if not factor_id:
                _mark("mfa_challenge", False, "缺少 factor id")
                return {"ok": False, "error": "mfa_challenge_no_factor", "detail": login_data, "steps": step}
            import pyotp
            from core.openai_auth import validate_mfa_totp
            referer = str(login_data.get("continue_url") or f"{AUTH_BASE}/mfa-challenge")
            continue_url = validate_mfa_totp(
                session, pyotp.TOTP(totp_secret).now(), factor_id, referer=referer)
            code = _follow_to_code(session, continue_url)
            _mark("mfa_challenge", True, code[:8] + "***")
        elif not code and page_type == "email_otp_verification":
            _mark("email_otp", False, "需要邮箱验证码（暂不支持导入账号收码）")
            return {"ok": False, "error": "need_email_otp", "detail": login_data, "steps": step}
        elif not code:
            continue_url = str(login_data.get("continue_url") or "").strip()
            if continue_url:
                try:
                    code = _follow_to_code(session, continue_url)
                except Exception as exc:
                    logger.warning("[password-login] 跟随 continue_url 失败: %s", str(exc)[:200])

        if not code:
            _mark("authorization_code", False, "未取得")
            return {"ok": False, "error": "no_auth_code", "detail": login_data, "steps": step}
        _mark("authorization_code", True)

        # ⑤ code 换 token（复用 Codex 同一端点/client/redirect）
        from core.codex_oauth import exchange_codex_token
        token_resp = exchange_codex_token(session, code, code_verifier)
        access_token = str(token_resp.get("access_token") or "")
        refresh_token = str(token_resp.get("refresh_token") or "")
        id_token = str(token_resp.get("id_token") or "")
        if not access_token or not refresh_token:
            _mark("token_exchange", False, "响应缺少 AT/RT")
            return {"ok": False, "error": "token_exchange_failed",
                    "detail": {k: (v[:12] + "...") if isinstance(v, str) else v for k, v in token_resp.items()},
                    "steps": step}
        _mark("token_exchange", True, f"at_len={len(access_token)} rt_rotated=1")

        payload = _jwt_payload(access_token)
        profile = payload.get("https://api.openai.com/profile") or {}
        auth_claim = payload.get("https://api.openai.com/auth") or {}
        result = {
            "ok": True,
            "email": str(profile.get("email") or target),
            "account_id": str(auth_claim.get("chatgpt_account_id") or ""),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "expires_at": payload.get("exp"),
            "elapsed_ms": int((time.time() - t0) * 1000),
            "steps": step,
        }

        # ⑥ 可选 CAS 写回
        if write_back:
            try:
                from core import db
                old = db.get_account_by_email(target) or {}
                # CAS 校验的是顶层 access_token（update_account_chatgpt_oauth 语义），优先用它
                old_at = str(old.get("access_token") or old.get("chatgpt_oauth_access_token") or "")
                wb = db.update_account_chatgpt_oauth(
                    target,
                    {
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                        "oauth_client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                        "source": "password_login",
                        "expires_at": payload.get("exp"),
                    },
                    expected_access_token=old_at,
                )
                result["write_back"] = wb
            except Exception as exc:
                result["write_back"] = {"updated": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return result
    except Exception as exc:
        logger.exception("[password-login] 异常")
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "elapsed_ms": int((time.time() - t0) * 1000), "steps": step}
    finally:
        try:
            session.close()
        except Exception:
            pass


if __name__ == "__main__":
    # 最小自检：PKCE / code 提取 / error payload 解析（离线）
    v, c = _generate_pkce()
    assert 80 < len(v) < 100 and len(c) == 43, (len(v), len(c))
    assert _extract_code("https://platform.openai.com/auth/callback?code=abc123&state=x") == "abc123"
    assert _extract_code("https://auth.openai.com/log-in") == ""
    import base64 as _b64
    _p = _b64.b64encode(json.dumps({"errorCode": "rate_limit_exceeded"}).encode()).decode()
    ec, _pl = _parse_error_page(f"https://auth.openai.com/error?payload={_p}")
    assert ec == "rate_limit_exceeded", ec
    print("password_login self-check OK")
