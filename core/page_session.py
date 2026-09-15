# -*- coding: utf-8 -*-
"""页面会话：用 CloakBrowser 页面充当 HTTP 会话（"协议调用一下浏览器"）。

设计动机
--------
纯协议请求缺 `openai-sentinel-so-token`（服务端会挂起 30s+）。内核浏览器页面里
的 SentinelSDK 能稳定生成 token + so，因此把关键请求改为"页内 fetch"：
cookie 天然一致、sender 由页面生成，同时不需要跑完整 UI 交互（不等渲染/找元素）。

用法
----
    driver, opened = build_cloak_driver(proxy=...)
    session = PageSession(driver)
    driver.get(auth_url)              # 真实导航过 Cloudflare
    session.post(...authorize/continue..., data=json.dumps({...}))
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 需要 openai-sentinel-token / openai-sentinel-so-token 的 POST 端点。
SENTINEL_POST_PATHS = (
    "/api/accounts/authorize/continue",
    "/api/accounts/email-otp/validate",
    "/api/accounts/add-phone/send",
    "/api/accounts/phone-otp/validate",
    "/api/accounts/user/register",
    "/api/accounts/create_account",
)

_FETCH_JS = r"""
const done = arguments[arguments.length - 1];
const p = arguments[0];
(async () => {
  const t0 = Date.now();
  try {
    const h = Object.assign({}, p.headers || {});
    if (p.needSentinel && window.SentinelSDK) {
      const flow = p.flow || "authorize_continue";
      const tok = await window.SentinelSDK.token(flow);
      const so = await window.SentinelSDK.sessionObserverToken(flow);
      h["openai-sentinel-token"] = typeof tok === "string" ? tok : JSON.stringify(tok);
      if (so) h["openai-sentinel-so-token"] = typeof so === "string" ? so : JSON.stringify(so);
    }
    const r = await fetch(p.url, {
      method: p.method || "GET",
      headers: h,
      body: p.body || undefined,
      credentials: "include",
      redirect: "follow",
    });
    const text = await r.text();
    done({ ok: true, ms: Date.now() - t0, status: r.status, url: r.url, text: text });
  } catch (e) {
    done({ ok: false, ms: Date.now() - t0, error: String((e && e.message) || e) });
  }
})();
"""


class PageResponse:
    """最小 requests.Response 兼容层（codex_oauth 只用到这几个属性）。"""

    def __init__(self, payload: dict[str, Any]):
        self.status_code = int(payload.get("status") or 0)
        self.url = str(payload.get("url") or "")
        self.text = str(payload.get("text") or "")
        self.elapsed_ms = int(payload.get("ms") or 0)

    def json(self) -> dict:
        try:
            data = json.loads(self.text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:200]}")


class _SessionProxy:
    """模拟 curl_cffi Session 的结构（codex_oauth 会访问 session.session.cookies.*）。"""

    def __init__(self, page_session: "PageSession"):
        self.cookies = _PageCookiesAdapter(page_session)


class _CookieItem:
    __slots__ = ("name", "value")

    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value


class _PageCookiesAdapter:
    """把页面 cookie 适配成 curl_cffi 风格（jar 可迭代 + get），供 codex_oauth 读取。"""

    def __init__(self, page_session: "PageSession"):
        self._ps = page_session

    def _items(self) -> list:
        items: list = []
        try:
            for c in self._ps.page.context.cookies():
                items.append(_CookieItem(str(c.get("name") or ""), str(c.get("value") or "")))
        except Exception:
            pass
        if not items:
            try:
                raw = self._ps.driver.execute_script("return document.cookie;") or ""
            except Exception:
                raw = ""
            for part in str(raw).split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    items.append(_CookieItem(k.strip(), v.strip()))
        return items

    @property
    def jar(self) -> list:
        seen: dict = {}
        for item in self._items():
            cur = seen.get(item.name)
            if cur is None or len(item.value) > len(cur.value):
                seen[item.name] = item
        return list(seen.values())

    def get(self, name: str, default=None):
        # 同名 cookie 可能有多条（不同 path/domain）；已登录会话的那条更长，取最长值。
        best = None
        for item in self._items():
            if item.name == name and (best is None or len(item.value) > len(best)):
                best = item.value
        return best if best is not None else default

    def __iter__(self):
        return iter(self._items())


class PageSession:
    """用内核浏览器页面代发 HTTP 的会话对象（兼容 BrowserSession 的请求接口子集）。"""

    page_transport = True

    def __init__(self, driver: Any, flow_default: str = "authorize_continue"):
        self.driver = driver
        self.page = getattr(driver, "page", None)
        self._flow = str(flow_default or "authorize_continue")
        self.device_id = self._read_device_id()
        self._fallback_session = None
        self.session = _SessionProxy(self)

    def _fallback(self):
        """缺失的 header/格式方法委托给 BrowserSession（纯本地计算，不发请求）。"""
        if self._fallback_session is None:
            from core.session import BrowserSession

            self._fallback_session = BrowserSession(proxy="")
        return self._fallback_session

    _SELF_ATTRS = frozenset({"driver", "page", "session", "_flow", "device_id", "_fallback_session"})

    def __getattr__(self, name: str):
        # 仅当本类没有该属性时触发；get/post 等已实现的方法不受影响。
        # codex_oauth 会用到 BrowserSession 的若干工具方法（含 _get_common_headers），
        # 全部委托给 fallback 会话（纯本地计算，不发网络请求）。
        if name.startswith("__") or name in PageSession._SELF_ATTRS:
            raise AttributeError(name)
        return getattr(self._fallback(), name)

    # ---- 内部 ----
    def _read_device_id(self) -> str:
        try:
            value = self.driver.execute_script(
                "return (document.cookie.match(/(?:^|; )oai-did=([^;]+)/) || [])[1] || '';"
            )
        except Exception:
            value = ""
        return str(value or "")

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        data: str | None = None,
        allow_redirects: bool = True,
        flow: str | None = None,
    ) -> PageResponse:
        need_sentinel = method.upper() == "POST" and any(p in url for p in SENTINEL_POST_PATHS)
        payload = {
            "method": method.upper(),
            "url": url,
            "headers": headers or {},
            "body": data,
            "needSentinel": need_sentinel,
            "flow": flow or self._flow,
        }
        result = self.driver.execute_async_script(_FETCH_JS, payload)
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("error") or "unknown"
            raise RuntimeError(f"[PageSession] {method} {url[:80]} 失败: {reason}")
        resp = PageResponse(result)
        logger.info("[PageSession] %s %s -> %s (%sms)", method.upper(), url[:70], resp.status_code, resp.elapsed_ms)
        return resp

    # ---- 兼容 BrowserSession 的接口 ----
    def get(self, url: str, headers: dict | None = None, allow_redirects: bool = True, **_: Any) -> PageResponse:
        return self._request("GET", url, headers=headers, allow_redirects=allow_redirects)

    def post(self, url: str, headers: dict | None = None, data: str | None = None, allow_redirects: bool = False, **_: Any) -> PageResponse:
        return self._request("POST", url, headers=headers, data=data, allow_redirects=allow_redirects)

    def get_auth_headers(self, referer: str = "") -> dict:
        headers = {"content-type": "application/json"}
        if referer:
            headers["referer"] = referer
        return headers

    def get_auth_navigate_headers(self, referer: str = "") -> dict:
        return {"referer": referer} if referer else {}

    def auth_cookie_header(self) -> str:
        return f"oai-did={self.device_id}" if self.device_id else ""

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass
