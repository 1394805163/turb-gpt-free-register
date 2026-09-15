# -*- coding: utf-8 -*-
"""短调用实验 E2：登录态 harvest -> 关浏览器 -> 协议读 backend-api（验证登录态可离线复用）。

流程（待主人放行后对指定账号执行）：
  1. 目标账号 OTP 登录（SG 出口/账号画像种子）
  2. 读 session 拿 AT；到 auth.openai.com 页面 harvest SentinelSDK token/so；导出 cookies
  3. 关闭浏览器
  4. curl_cffi(impersonate=chrome146) 请求：
       a) GET https://chatgpt.com/api/auth/session   （纯 cookie 态）
       b) GET https://chatgpt.com/backend-api/me     （Bearer AT）
     记录状态码/耗时/摘要

用法：
    python tools/probe_shortcall_backend.py --email <target>
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("probe_shortcall_backend")

HARVEST_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const sdk = window.SentinelSDK;
  const out = {sdk: !!sdk};
  try {
    if (!sdk) return done(out);
    const flow = "authorize_continue";
    const tok = await sdk.token(flow);
    const so = await sdk.sessionObserverToken(flow);
    out.token = typeof tok === "string" ? tok : JSON.stringify(tok);
    out.so = so ? (typeof so === "string" ? so : JSON.stringify(so)) : "";
    out.ua = navigator.userAgent;
    done(out);
  } catch (e) { out.error = String((e && e.message) || e); done(out); }
})();
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="目标账号邮箱")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()
    email = str(args.email or "").strip()

    from core.account_password import _resolve_fresh_account_route
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

    result: dict = {"email": email}
    driver = None
    try:
        proxy, proxy_selection = _resolve_fresh_account_route(email)
        driver, opened = build_cloak_driver(
            proxy=proxy,
            proxy_selection=proxy_selection,
            fingerprint_seed=account_fingerprint_seed(email),
        )
        driver.set_page_load_timeout(90)

        # 1) OTP 登录
        driver.get("https://chatgpt.com/auth/login")
        _maybe_accept(driver)
        time.sleep(2)
        login_otp_after = time.time()
        _submit_email_and_wait_next(driver, email, attempts=2, timeout=120)
        sess = OtpWaitSession(wait_fn=wait_for_otp)
        code = sess.wait(email, after_ts=login_otp_after, max_wait=90)
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        _click_continue(driver)
        _wait_after_email_otp_submit(driver, timeout=25)
        time.sleep(2)
        info = _fetch_chatgpt_session(driver, timeout=90)
        at = str(info.get("accessToken") or "")
        result["at_len"] = len(at)
        if not at:
            raise RuntimeError("登录后未拿到 accessToken")
        logger.info("[E2] 登录成功，AT len=%s", len(at))

        # 2) 到 auth 页 harvest（SDK 只在认证域出现）+ 导出 cookies
        driver.get("https://auth.openai.com/log-in")
        time.sleep(5)
        harv = driver.execute_async_script(HARVEST_JS) or {}
        cookies = driver.page.context.cookies()
        jar = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
        result.update({
            "harvest_sdk": bool(harv.get("sdk")),
            "token_len": len(harv.get("token") or ""),
            "so_len": len(harv.get("so") or ""),
            "cookie_count": len(jar),
        })
        logger.info("[E2] harvest：sdk=%s token_len=%s so_len=%s cookies=%s",
                    result["harvest_sdk"], result["token_len"], result["so_len"], result["cookie_count"])
        ua = str(harv.get("ua") or "")

        # 3) 关浏览器
        driver.quit()
        driver = None
        logger.info("[E2] 浏览器已关闭，转协议请求")

        from curl_cffi import requests as curl_requests

        s = curl_requests.Session(impersonate="chrome146")
        base_headers = {"accept": "application/json", "user-agent": ua or "Mozilla/5.0"}

        t0 = time.time()
        r1 = s.get("https://chatgpt.com/api/auth/session", headers=base_headers, cookies=jar, timeout=args.timeout)
        result["session_status"] = r1.status_code
        result["session_ms"] = int((time.time() - t0) * 1000)
        result["session_has_at"] = "accessToken" in (r1.text or "")

        t0 = time.time()
        r2 = s.get("https://chatgpt.com/backend-api/me",
                   headers={**base_headers, "authorization": f"Bearer {at}"},
                   cookies=jar, timeout=args.timeout)
        result["me_status"] = r2.status_code
        result["me_ms"] = int((time.time() - t0) * 1000)
        result["me_body"] = (r2.text or "")[:240]

        print("RESULT:", json.dumps(result, ensure_ascii=False))
        return 0 if (r1.status_code == 200 and r2.status_code == 200) else 1
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        print("RESULT:", json.dumps(result, ensure_ascii=False))
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
