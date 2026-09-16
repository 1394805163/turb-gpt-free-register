# -*- coding: utf-8 -*-
r"""样品B：短调用注册实验 —— 浏览器只调一次（真实留痕+harvest）→ 关浏览器 → 纯协议跑完注册。

对比组：
- cloak（全程 UI 注册）：今天 21 个（20 活 / 1 死）
- protocol_page（浏览器全程当代发载体）：283
- 本实验 shortcall：浏览器只开一次（访问 chatgpt/auth 留痕 + harvest cookies/UA），
  关闭后全部用 curl_cffi 协议（sentinel 由 Node 沙箱补）完成注册。

用法：
    python tools/probe_shortcall_register.py
    python tools/probe_shortcall_register.py --keep-browser   # 调试：保留浏览器
"""
import argparse
import json
import logging
import os
import random
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("shortcall")

HARVEST_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const out = {ua: navigator.userAgent, lang: navigator.language,
               langs: (navigator.languages || []).join(","),
               tz: Intl.DateTimeFormat().resolvedOptions().timeZone};
  try {
    const sdk = window.SentinelSDK;
    out.sdk = !!sdk;
    if (sdk) {
      const t = await sdk.token("authorize_continue");
      out.token = typeof t === "string" ? t : JSON.stringify(t);
      try {
        const so = await sdk.sessionObserverToken("authorize_continue");
        out.so = so ? (typeof so === "string" ? so : JSON.stringify(so)) : "";
      } catch (e) {}
    }
  } catch (e) { out.sdk_error = String((e && e.message) || e); }
  done(out);
})();
"""


def harvest(driver) -> dict:
    """从浏览器采集 cookies + 环境参数（UA/语言/时区）+ 可选 sentinel token。"""
    res = driver.execute_async_script(HARVEST_JS) or {}
    cookies = []
    try:
        for c in driver.page.context.cookies() or []:
            cookies.append({
                "name": c.get("name"), "value": c.get("value"),
                "domain": c.get("domain") or "", "path": c.get("path") or "/",
            })
    except Exception as exc:
        logger.warning("cookies harvest 失败: %s", exc)
    res["cookies"] = cookies
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-browser", action="store_true", help="调试：不关浏览器")
    ap.add_argument("--email", default="", help="指定邮箱（默认从池领取）")
    ap.add_argument("--node", default="", help="手动指定 mihomo 节点（fuzzy，如 SG04），跳过随机选择")
    args = ap.parse_args()

    from config import proxy as proxy_cfg
    from core.cloakbrowser_driver import account_fingerprint_seed, build_cloak_driver
    from core.email_provider import (
        acquire_email_after_input, release_email_if_unconsumed, resolve_email_source, wait_for_otp,
    )
    from core.session import BrowserSession

    email = args.email.strip() or acquire_email_after_input("")
    seed = account_fingerprint_seed(email)
    email_source = resolve_email_source(email)
    logger.info("目标邮箱: %s | seed=%s", email, seed)

    proxy_selection = proxy_cfg.pick_registration_proxy() or {}
    if args.node:
        import requests as _rq
        from urllib.parse import quote as _q
        base = str(getattr(proxy_cfg, "MIHOMO_CONTROLLER_URL", "")).rstrip("/")
        grp = str(proxy_selection.get("group") or getattr(proxy_cfg, "MIHOMO_US_GROUP", "") or "")
        try:
            hdrs = proxy_cfg._mihomo_headers(
                proxy_cfg.MIHOMO_CONTROLLER_SECRET,
                username=getattr(proxy_cfg, "MIHOMO_CONTROLLER_USERNAME", ""),
                password=getattr(proxy_cfg, "MIHOMO_CONTROLLER_PASSWORD", ""),
            )
            allnames = _rq.get(f"{base}/proxies/{_q(grp, safe='')}", headers=hdrs, timeout=8).json().get("all") or []
            match = next((n for n in allnames if args.node in str(n)), args.node)
            _rq.put(f"{base}/proxies/{_q(grp, safe='')}",
                    headers={**hdrs, "Content-Type": "application/json"},
                    json={"name": match}, timeout=8).raise_for_status()
            proxy_selection.update({"node_name": match, "group": grp, "mode": "mihomo_excluded", "transparent": True})
            logger.info("[短调用] 已手动切节点: %s", match)
        except Exception as exc:
            logger.warning("[短调用] 手动切节点失败: %s", str(exc)[:140])
    proxy_url = str(proxy_selection.get("proxy_url") or "")
    logger.info("出口: node=%s mode=%s transparent=%s",
                proxy_selection.get("node_name") or "-",
                proxy_selection.get("mode") or "-",
                proxy_selection.get("transparent"))

    result: dict = {"email": email, "ok": False, "stage": "init", "harvest": {}}
    t_start = time.time()
    session = None
    harv: dict = {}

    try:
        # ── 1) 浏览器只调一次：真实留痕 + harvest ─────────────────────
        logger.info("[短调用] 启动浏览器（唯一一次）...")
        driver = None
        try:
            driver, _opened = build_cloak_driver(
                proxy=proxy_url,
                proxy_selection=proxy_selection,
                fingerprint_seed=seed,
            )
            # 预热浏览（与 cloak 注册流程同一套人类行为）：
            # 先逛首页拿 cf_clearance/埋点 cookie 并产生滚动/鼠标行为，再依次进登录页/认证页。
            from core.humanize import delay as human_delay
            from core.roxy_registration import _maybe_accept, _page_warmup

            normal_timeout = 90
            warm_timeout = 25

            def _nav(url: str, label: str) -> bool:
                try:
                    driver.set_page_load_timeout(warm_timeout)
                    try:
                        driver.get(url)
                    finally:
                        driver.set_page_load_timeout(normal_timeout)
                    human_delay("navigate")
                    _maybe_accept(driver)
                    _page_warmup(driver, reason=label)
                    return True
                except Exception as exc:
                    logger.info("[短调用] 预热访问 %s 失败（继续后续步骤）：%s", label, str(exc)[:110])
                    try:
                        driver.set_page_load_timeout(normal_timeout)
                    except Exception:
                        pass
                    return False

            if _nav("https://chatgpt.com/", "homepage"):
                try:
                    driver.execute_script(
                        "window.scrollTo({top: Math.round(300 + Math.random() * 700), behavior: 'smooth'});"
                    )
                except Exception:
                    pass
                human_delay("page_warmup")
            _nav("https://chatgpt.com/auth/login", "login")
            human_delay("form")
            _nav("https://auth.openai.com/log-in", "auth")
            human_delay("form")
            harv = harvest(driver)
            try:
                driver.get("https://chatgpt.com/cdn-cgi/trace")
                time.sleep(1)
                body = driver.find_element("tag name", "body").text
                trace_map = dict(l.split("=", 1) for l in (body or "").splitlines() if "=" in l)
                harv["trace"] = {"ip": trace_map.get("ip"), "loc": trace_map.get("loc")}
                logger.info("[短调用] 浏览器侧 chatgpt.com 出口: ip=%s loc=%s",
                            trace_map.get("ip"), trace_map.get("loc"))
            except Exception as exc:
                harv["trace"] = {"error": str(exc)[:90]}
        finally:
            if driver is not None and not args.keep_browser:
                try:
                    driver.quit()
                except Exception:
                    pass
                logger.info("[短调用] 浏览器已关闭 → 剩下全走协议")
        result["harvest"] = {
            "cookies": len(harv.get("cookies") or []),
            "ua": str(harv.get("ua") or "")[:90],
            "lang": harv.get("lang"),
            "tz": harv.get("tz"),
            "token_len": len(str(harv.get("token") or "")),
            "so_len": len(str(harv.get("so") or "")),
            "sdk": harv.get("sdk"),
        }
        logger.info("[短调用] harvest: %s", json.dumps(result["harvest"], ensure_ascii=False))
        if not harv.get("cookies"):
            raise RuntimeError("harvest 未取到 cookies")

        # ── 2) 协议会话：注入 cookies + 环境对齐 ──────────────────────
        session = BrowserSession(proxy=proxy_url, fingerprint_seed=(seed or None))
        injected = 0
        for c in harv.get("cookies") or []:
            try:
                session.session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain") or None, path=c.get("path") or "/",
                )
                injected += 1
            except Exception:
                pass
        if harv.get("ua"):
            session.browser_profile["user_agent"] = str(harv["ua"])
        if harv.get("lang"):
            session.browser_profile["accept_language"] = str(harv["lang"]) + ",en;q=0.9"
        logger.info("[短调用] 协议会话注入 cookies=%s UA=%s", injected, str(harv.get("ua"))[:80])

        # ── 3) 纯协议注册流程（照 main.py protocol 分支）────────────────
        from core.openai_auth import (
            request_sentinel_token, build_sentinel_header,
            send_email_otp, validate_email_otp, create_account, navigate_about_you,
        )
        from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
        from core.account_export import (
            create_batch_archive_dir, fetch_session, follow_oauth_callback, save_account_data,
        )
        from core.name_samples import random_display_name
        from core.profile_utils import generate_random_birthday

        result["stage"] = "protocol"
        try:
            tr = session.get("https://chatgpt.com/cdn-cgi/trace", headers={"accept": "*/*"})
            tm = dict(l.split("=", 1) for l in (tr.text or "").splitlines() if "=" in l)
            logger.info("[短调用] 协议侧 chatgpt.com 出口: ip=%s loc=%s", tm.get("ip"), tm.get("loc"))
            result["protocol_trace_ip"] = tm.get("ip")
        except Exception as exc:
            logger.warning("[短调用] 协议 trace 失败: %s", str(exc)[:100])
        get_providers(session)
        csrf = get_csrf_token(session)
        authorize_url = signin_openai(session, csrf, email)
        otp_after = time.time()

        # 4) authorize 导航（自己控制落点：OTP 分支 / 密码注册分支）
        nav_headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
        resp = session.get(authorize_url, headers=nav_headers, allow_redirects=True)
        final_url = str(getattr(resp, "url", "") or "")
        logger.info("[短调用] authorize 落点: %s", final_url[:120])

        registration_password = ""
        if "/create-account/password" in final_url:
            # 服务端把协议流导向"密码注册"分支：先设密码，再走邮箱 OTP
            from core.roxy_registration import _registration_password
            registration_password = _registration_password()
            sent_pw = request_sentinel_token(session, "username_password_create")
            th_pw, sh_pw = build_sentinel_header(session, sent_pw, "username_password_create")
            pw_headers = session.get_auth_headers(referer="https://auth.openai.com/create-account/password")
            pw_headers["openai-sentinel-token"] = th_pw
            if sh_pw:
                pw_headers["openai-sentinel-so-token"] = sh_pw
            r_pw = session.post(
                "https://auth.openai.com/api/accounts/user/register",
                headers=pw_headers,
                data=json.dumps({"username": email, "password": registration_password}),
            )
            if r_pw.status_code != 200:
                raise RuntimeError("user/register HTTP %s: %s" % (r_pw.status_code, (r_pw.text or "")[:180]))
            d_pw = r_pw.json() or {}
            page_pw = d_pw.get("page") or {}
            if str(page_pw.get("type") or "") in {"email_otp_send", "email_otp_send_registration"} or \
                    "email-otp/send" in str(d_pw.get("continue_url") or ""):
                session.get(
                    "https://auth.openai.com/api/accounts/email-otp/send",
                    headers=session.get_auth_navigate_headers(referer="https://auth.openai.com/create-account/password"),
                    allow_redirects=True,
                )
            logger.info("[短调用] 密码注册分支：密码已提交(len=%s)，转入邮箱验证", len(registration_password))
        elif "email-verification" not in final_url and "log-in" in final_url:
            # 落点仍在登录页：显式提交邮箱（带 harvest 的 token）
            submit_headers = session.get_auth_headers(referer="https://auth.openai.com/log-in")
            if harv.get("token"):
                submit_headers["openai-sentinel-token"] = str(harv["token"])
            if harv.get("so"):
                submit_headers["openai-sentinel-so-token"] = str(harv["so"])
            r1 = session.post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                headers=submit_headers,
                data=json.dumps({"username": {"kind": "email", "value": email}}),
            )
            if r1.status_code != 200:
                raise RuntimeError("authorize/continue HTTP %s: %s" % (r1.status_code, (r1.text or "")[:180]))
            p1 = str((((r1.json() or {}).get("page") or {}).get("type")) or "")
            logger.info("[短调用] 邮箱已提交 page=%s", p1)
            if p1 == "create_account_password":
                raise RuntimeError("邮箱提交后被导向密码分支（需重启流程走密码注册）")

        code = ""
        for attempt in range(1, 4):
            try:
                code = wait_for_otp(email, after_ts=otp_after)
                break
            except Exception as exc:
                if attempt >= 3:
                    raise
                logger.warning("[短调用] OTP 等待超时(%s/3)，重发后继续", attempt)
                otp_after = time.time()
                try:
                    send_email_otp(session)
                except Exception:
                    pass
        sent_v = request_sentinel_token(session, "authorize_continue")
        th_v, sh_v = build_sentinel_header(session, sent_v, "authorize_continue")
        logger.info("[短调用] OTP validate 带 sentinel 提交（so=%s）", bool(sh_v))
        validate_result = validate_email_otp(session, code, th_v, sh_v)
        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        cont = str(
            validate_result.get("continue_url") or validate_result.get("url")
            or page.get("continue_url") or page.get("url") or ""
        )
        logger.info("[短调用] OTP 通过 page_type=%s cont=%s", page_type, cont[:80])

        name = random_display_name()
        birthday = generate_random_birthday()
        if page_type in ("about_you", "about-you") or "about-you" in cont:
            navigate_about_you(session, cont or None)
            sent = request_sentinel_token(session, "oauth_create_account")
            th, sh = build_sentinel_header(session, sent, "oauth_create_account")
            created = create_account(session, name, birthday, th, sh)
            cont2 = str((created or {}).get("continue_url") or "")
            if not cont2:
                raise RuntimeError("create_account 无 continue_url: %s" % str(created)[:200])
            cont = cont2
        elif not cont:
            raise RuntimeError("OTP 后无 continue_url: %s" % str(validate_result)[:200])

        follow_oauth_callback(session, cont, referer="https://auth.openai.com/email-verification")
        info = fetch_session(session) or {}
        at = str(info.get("accessToken") or "")
        if not at:
            raise RuntimeError("未拿到 accessToken")

        # ── 4) 落库 ─────────────────────────────────────────────────
        batch_dir = create_batch_archive_dir(1, 1)
        extra = {
            "user": info.get("user"), "account": info.get("account"),
            "expires": info.get("expires"), "name": name, "birthday": birthday,
            "device_id": session.device_id, "browser_profile": session.browser_profile,
            "cloak_profile_seed": seed, "shortcall_experiment": True,
            "harvest_summary": result.get("harvest"),
        }
        row_id = save_account_data(
            email=email, access_token=at, totp_secret=None, extra=extra,
            email_source=email_source,
            proxy_used=proxy_selection.get("node_name") or proxy_url,
            batch_dir=batch_dir,
        )
        if registration_password:
            try:
                from core import db as _db
                _db.update_account_registration_password(email, registration_password)
            except Exception as exc:
                logger.warning("[短调用] 注册密码落盘失败: %s", str(exc)[:120])
        result.update({"ok": True, "stage": "done", "row_id": row_id,
                       "at_len": len(at), "has_password": bool(registration_password),
                       "elapsed_s": round(time.time() - t_start, 1)})
        logger.info("[短调用] ✅ 注册成功 row_id=%s elapsed=%.1fs", row_id, time.time() - t_start)
        return 0
    except Exception as exc:
        result.update({
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:260]),
            "elapsed_s": round(time.time() - t_start, 1),
        })
        logger.error("[短调用] ❌ 失败: %s", result["error"])
        try:
            release_email_if_unconsumed(email, note="shortcall 失败: %s" % str(exc)[:80])
        except Exception:
            pass
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        try:
            os.makedirs("run", exist_ok=True)
            out = os.path.join("run", "shortcall-register-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
            logger.info("[短调用] 结果已写入 %s", out)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
