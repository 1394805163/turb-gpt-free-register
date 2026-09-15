# -*- coding: utf-8 -*-
"""短调用实验：harvest SentinelSDK token/so -> 关浏览器 -> curl_cffi 带凭据发 authorize/continue。

验证目标（"协议只是调用一下内核浏览器"的路线）：
  1. 浏览器里能 harvest openai-sentinel-token / openai-sentinel-so-token（无 UI 交互）
  2. 关闭浏览器后，curl_cffi(impersonate=chrome146) + 导出 cookies + 双 token
     直接发 auth.openai.com 的 authorize/continue 也能被服务端接受
  3. --mode no-so 对照组（不带 so-token）观察耗时/响应差异

不涉及任何已注册账号：提交的是假邮箱（probe 会话，不消耗邮箱池）。
用法：
    python tools/probe_shortcall_authorize.py                # 带 so-token 主验证
    python tools/probe_shortcall_authorize.py --mode no-so   # 对照组
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
logger = logging.getLogger("probe_shortcall")

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


def harvest(driver) -> dict:
    res = driver.execute_async_script(HARVEST_JS) or {}
    if not res.get("token"):
        raise RuntimeError(f"harvest 失败：{json.dumps(res, ensure_ascii=False)[:300]}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["with-so", "no-so"], default="with-so")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    from core.cloakbrowser_driver import build_cloak_driver
    from core.live_check_service import _resolve_live_check_route
    from core.page_session import PageSession
    from core.chatgpt_auth import signin_openai

    probe_email = f"shortcall-probe-{int(time.time())}@example.com"
    driver = None
    result: dict = {"mode": args.mode, "email": probe_email}
    try:
        route = _resolve_live_check_route(None, country_hint="SG")
        driver, opened = build_cloak_driver(
            proxy=route.get("proxy"),
            proxy_selection=route.get("proxy_selection"),
        )
        driver.set_page_load_timeout(60)

        driver.get("https://chatgpt.com/auth/login")
        time.sleep(8)

        session = PageSession(driver)
        csrf_resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
        )
        csrf = str((csrf_resp.json() or {}).get("csrfToken") or "")
        if not csrf:
            raise RuntimeError("未取得 csrfToken")
        auth_url = signin_openai(session, csrf, probe_email, prompt="login_or_signup")
        logger.info("已取得 authorize URL：%s", auth_url[:160])

        driver.get(auth_url)
        time.sleep(6)

        harv = harvest(driver)
        cookies = driver.page.context.cookies()
        jar = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
        ua = harv.get("ua") or ""
        result.update({
            "token_len": len(harv.get("token") or ""),
            "so_len": len(harv.get("so") or ""),
            "cookie_count": len(jar),
            "ua": ua[:90],
        })
        logger.info("harvest 完成：token_len=%s so_len=%s cookies=%s",
                    result["token_len"], result["so_len"], result["cookie_count"])

        # 关浏览器（短调用结束）
        driver.quit()
        driver = None
        logger.info("浏览器已关闭，改用 curl_cffi 发协议请求")

        from curl_cffi import requests as curl_requests

        s = curl_requests.Session(impersonate="chrome150")
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://auth.openai.com",
            "referer": "https://auth.openai.com/log-in",
            "user-agent": ua or "Mozilla/5.0",
            "openai-sentinel-token": harv.get("token") or "",
        }
        if args.mode == "with-so":
            headers["openai-sentinel-so-token"] = harv.get("so") or ""

        body = json.dumps({"username": {"kind": "email", "value": probe_email}})
        t0 = time.time()
        try:
            resp = s.post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                data=body,
                headers=headers,
                cookies=jar,
                timeout=args.timeout,
            )
            elapsed = time.time() - t0
            result.update({
                "status": resp.status_code,
                "elapsed_s": round(elapsed, 2),
                "body": (resp.text or "")[:400],
            })
        except Exception as exc:
            result.update({"error": f"{type(exc).__name__}: {str(exc)[:220]}", "elapsed_s": round(time.time() - t0, 2)})

        print("RESULT:", json.dumps(result, ensure_ascii=False))
        return 0
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
