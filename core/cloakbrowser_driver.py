# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)


def _proxy_log_label(proxy_url: str | None) -> str:
    """生成不含账号、密码和 sticky identity 的代理日志标签。"""
    if not proxy_url:
        return "无"
    try:
        parsed = urlsplit(str(proxy_url))
        if not parsed.scheme or not parsed.hostname:
            return "已配置"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except (TypeError, ValueError):
        return "已配置"


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press("Meta+A")
            self.page.keyboard.press("Backspace")

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    @property
    def text(self) -> str:
        """Selenium-compatible visible text used by OTP/button helpers."""
        try:
            if self.locator is not None:
                return str(self.locator.inner_text(timeout=1500) or "")
            return str(self.handle.inner_text() or "")
        except Exception:
            try:
                return str(self._eval("el => el.innerText || el.textContent || ''") or "")
            except Exception:
                return ""

    def send_keys(self, *values: str) -> None:
        # 兼容 Selenium: el.send_keys(Keys.COMMAND, 'a')。
        text = "".join(str(v or "") for v in values)
        lower = text.lower()

        # Selenium send_keys 不会在每段文本前重新鼠标点击；反复 click 会把光标
        # 移回输入框中部/开头，逐字符输入最终就会倒序。这里只聚焦，不移动光标。
        try:
            if self.locator is not None:
                self.locator.focus(timeout=3000)
            else:
                self.handle.focus(timeout=3000)
        except Exception:
            try:
                self._eval("el => el.focus()")
            except Exception:
                pass
        if "\ue03d" in text or "\ue009" in text or "command" in lower or "control" in lower:
            # Selenium Keys.CONTROL/COMMAND 编码可能传入私有区字符；这里按全选处理。
            import platform

            shortcut = "Meta+A" if platform.system().lower() == "darwin" else "Control+A"
            self.page.keyboard.press(shortcut)
            return
        special_keys = {
            "\ue003": "Backspace",
            "\ue004": "Tab",
            "\ue006": "Enter",
            "\ue007": "Enter",
            "\ue00c": "Escape",
            "\ue00d": "Space",
            "\ue012": "ArrowLeft",
            "\ue013": "ArrowUp",
            "\ue014": "ArrowRight",
            "\ue015": "ArrowDown",
            "\ue017": "Delete",
        }
        if text in special_keys:
            self.page.keyboard.press(special_keys[text])
            return
        try:
            # Playwright fill() 会替换整个 value；press_sequentially/type 才符合
            # Selenium 在当前光标位置追加文本的语义。
            if self.locator is not None:
                self.locator.press_sequentially(text, delay=35)
            else:
                self.handle.type(text, delay=35)
        except Exception:
            self.page.keyboard.type(text, delay=35)

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class CloakSeleniumDriver:
    """只实现本项目 Roxy Selenium 流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            return handle.json_value()
        except Exception as exc:
            msg = str(exc)
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                # 保留当前 Playwright page.url；上层状态机据此区分正常跨域跳转
                # 与长期无法完成的空白导航，而不是把两者都记成 unknown。
                return {
                    "ok": True,
                    "reason": "navigation_after_script",
                    "url": str(getattr(page, "url", "") or ""),
                    "inputs": [],
                }
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    return proxy.replace("socks5h://", "socks5://")


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _detect_openai_route_geo(proxy_url: str | None = None) -> dict:
    """从 OpenAI 同域 Cloudflare trace 检测真实路由出口。

    透明路由只匹配 OpenAI 域名；常规 IP 查询站会绕过该规则，因此不能用于
    判断注册请求的真实出口。
    """
    try:
        import requests
        from config import browser as _browser_cfg

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        response = requests.get(
            "https://auth.openai.com/cdn-cgi/trace",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
            proxies=proxies,
            timeout=(5, float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)),
        )
        if response.status_code != 200:
            return {}
        fields = {}
        for line in str(response.text or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip().lower()] = value.strip()
        country = str(fields.get("loc") or "").upper()
        colo = str(fields.get("colo") or "").upper()
        timezone = "America/Los_Angeles" if country == "US" else ""
        result = {
            "ip": fields.get("ip") or "",
            "country": country,
            "city": colo,
            "timezone": timezone,
            "source": "openai_cloudflare_trace",
        } if country else {}
        if result:
            logger.info(
                "[Cloak] OpenAI 路由出口地理信息：ip=%s country=%s colo=%s timezone=%s",
                result.get("ip") or "?", result.get("country") or "?",
                result.get("city") or "?", result.get("timezone") or "?",
            )
        return result
    except Exception as exc:
        logger.debug("[Cloak] OpenAI 路由出口检测失败：%s: %s", type(exc).__name__, exc)
        return {}


def _assert_mihomo_us_exit(selection: dict | None, geo: dict | None) -> None:
    data = selection if isinstance(selection, dict) else {}
    if str(data.get("mode") or "") != "mihomo_us":
        return
    country = str((geo or {}).get("country") or "").strip().upper()
    if not country:
        raise RuntimeError("无法确认 OpenAI 注册出口国家；已阻止继续注册")
    if country != "US":
        raise RuntimeError(f"OpenAI 注册出口非美国（country={country}）；已阻止继续注册")


def _assert_registration_exit_country(selection: dict | None, geo: dict | None) -> None:
    """执行 Resin/Mihomo 共享的出口国家策略；未知出口一律阻止注册。"""
    data = selection if isinstance(selection, dict) else {}
    allowed = {str(value).strip().upper() for value in (data.get("allowed_countries") or []) if str(value).strip()}
    excluded = {str(value).strip().upper() for value in (data.get("excluded_countries") or []) if str(value).strip()}
    if not allowed and not excluded:
        return
    country = str((geo or {}).get("country") or data.get("exit_country") or "").strip().upper()
    if not country:
        raise RuntimeError("无法确认注册出口国家；已阻止继续注册")
    common_countries = {"US", "JP", "TW", "SG", "KR"}
    allowed_other = "OTHER" in allowed and country not in common_countries
    if allowed and country not in (allowed - {"OTHER"}) and not allowed_other:
        raise RuntimeError(f"注册出口国家 {country} 不在允许地区内；已阻止继续注册")
    if country in excluded:
        raise RuntimeError(f"注册出口国家 {country} 属于排除地区；已阻止继续注册")


def _build_cloak_locale_options(
    proxy_url: str | None = None,
    *,
    transparent_route: bool = False,
    proxy_selection: dict | None = None,
) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        detected_geo = (
            _detect_openai_route_geo(proxy_url)
            if transparent_route
            else _detect_cloak_exit_geo(proxy_url)
        )
        selection = proxy_selection if isinstance(proxy_selection, dict) else {}
        fallback_geo = {}
        for source in (selection.get("exit_geo"), selection.get("preflight")):
            if isinstance(source, dict):
                for key in ("ip", "country", "city", "region", "timezone", "org"):
                    if source.get(key) and not fallback_geo.get(key):
                        fallback_geo[key] = source.get(key)
        if selection.get("exit_country") and not fallback_geo.get("country"):
            fallback_geo["country"] = selection.get("exit_country")
        geo = dict(fallback_geo)
        geo.update({k: v for k, v in (detected_geo or {}).items() if v})
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def _allows_transparent_mihomo_route(selection: dict | None) -> bool:
    """允许已完成 Mihomo 节点选择的透明路由省略显式代理 URL。"""
    data = selection if isinstance(selection, dict) else {}
    return bool(data.get("transparent")) and data.get("mode") in {"mihomo_us", "mihomo_excluded"}


def build_cloak_driver(
    proxy: str | None = None,
    proxy_selection: dict | None = None,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None 且没有 proxy_selection 时选择一次代理；
    proxy_selection 由上游传入时沿用同一次节点选择，禁止二次轮换；
    proxy="..." 时使用指定代理。
    """
    from config import proxy as _proxy_cfg
    proxy_selection = dict(proxy_selection) if isinstance(proxy_selection, dict) else {}
    if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
        required = bool(getattr(_proxy_cfg, "REGISTRATION_PROXY_REQUIRED", False))
        if proxy_selection:
            proxy = str(proxy_selection.get("proxy_url") or proxy or "")
        elif proxy is None or not required:
            proxy_selection = _proxy_cfg.pick_registration_proxy()
            proxy = str(proxy_selection.get("proxy_url") or "")
        elif not str(proxy or "").strip():
            if required:
                raise RuntimeError("注册代理为空；已阻止直连")
    if bool(getattr(_proxy_cfg, "REGISTRATION_PROXY_REQUIRED", False)):
        if not bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
            raise RuntimeError("Resin 注册代理门禁已开启，但 CloakBrowser 代理开关未开启")
        if not str(proxy or "").strip():
            raise RuntimeError("Resin 注册代理门禁已开启，但合格代理池为空；已阻止 VPS 直连注册")
    else:
        if not bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
            raise RuntimeError("Resin 门禁已关闭但 CloakBrowser 代理开关未开启；已阻止直连注册")
        if not str(proxy or "").strip() and not _allows_transparent_mihomo_route(proxy_selection):
            raise RuntimeError("Mihomo 美国代理不可用；已阻止直连注册")
    try:
        from cloakbrowser import launch, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
    seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) else None
    transparent_route = _allows_transparent_mihomo_route(proxy_selection)
    locale_opts = _build_cloak_locale_options(
        proxy_url,
        transparent_route=transparent_route,
        proxy_selection=proxy_selection,
    )
    exit_geo = locale_opts.get("geo") if isinstance(locale_opts.get("geo"), dict) else {}
    _assert_registration_exit_country(proxy_selection, exit_geo)
    _assert_mihomo_us_exit(proxy_selection, exit_geo)
    if proxy_selection:
        proxy_selection["exit_ip"] = str(exit_geo.get("ip") or "")
        proxy_selection["exit_country"] = str(exit_geo.get("country") or "")
        proxy_selection["exit_geo"] = dict(exit_geo)
        logger.info(
            "[Cloak] 注册代理已锁定：mode=%s group=%s node=%s exit_ip=%s country=%s selection_ms=%s",
            proxy_selection.get("mode"), proxy_selection.get("group") or "-",
            proxy_selection.get("node_name") or "-", proxy_selection.get("exit_ip") or "?",
            proxy_selection.get("exit_country") or "?",
            proxy_selection.get("selection_ms") or 0,
        )
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)),
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if proxy_url:
        opts["proxy"] = proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key

    user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        _proxy_log_label(proxy_url), opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        locale_opts.get("accept_language") or "自动/默认", bool(user_data_dir),
    )
    context_kwargs = {}
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    if user_data_dir:
        context = launch_persistent_context(user_data_dir, **opts)
        page = context.new_page()
        browser = getattr(context, "browser", None) or context
        # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
    else:
        browser = launch(**opts)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

    driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
    # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
    # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Cloak注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    return driver, CloakOpenResult(raw={
        "driver": "cloakbrowser",
        "proxy": proxy_url,
        "proxy_mode": proxy_selection.get("mode") or "explicit",
        "proxy_group": proxy_selection.get("group") or "",
        "proxy_node": proxy_selection.get("node_name") or "",
        "proxy_exit_ip": proxy_selection.get("exit_ip") or "",
        "proxy_selection_ms": proxy_selection.get("selection_ms") or 0,
        "locale": locale_opts,
        "options": {k: v for k, v in opts.items() if k != "license_key"},
    })
