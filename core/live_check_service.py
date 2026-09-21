# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import _mask_proxy, open_plan_check_proxy, resolve_plan_check_route
from core.log_safety import redact_email
from core.pipeline_concurrency import PIPELINE_MAX_CONCURRENCY, pipeline_slot

logger = logging.getLogger(__name__)

_WORKERS = PIPELINE_MAX_CONCURRENCY
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _young_account_country_hint(account_id: int, *, max_age_hours: float = 6.0) -> str:
    """新注册账号（默认 6 小时内）查活时尽量固定注册时的出口国家。

    新号在几分钟内从 A 国注册、B 国登录、C 国查活，是典型的"账号被倒卖/接管"风控特征。
    """
    try:
        from config import proxy as proxy_cfg

        acc = db.get_account(int(account_id)) or {}
        created = str(acc.get("created_at") or "").strip()
        if not created:
            return ""
        from datetime import datetime as _dt

        age_hours = (_dt.now() - _dt.fromisoformat(created)).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            return ""
        # 注册国家来源：显式 proxy_exit_country -> registration_proxy.node_name -> 旧 proxy_used
        explicit = str(acc.get("proxy_exit_country") or "").strip().upper()
        registration_proxy = acc.get("registration_proxy")
        if len(explicit) != 2 or not isinstance(registration_proxy, dict):
            # 注册链路把出口信息写在 extra_json；顶层字段可能为空，这里补读。
            extra_raw = acc.get("extra_json")
            try:
                extra = json.loads(extra_raw) if isinstance(extra_raw, str) else (extra_raw if isinstance(extra_raw, dict) else {})
            except Exception:
                extra = {}
            if len(explicit) != 2:
                explicit = str(extra.get("proxy_exit_country") or "").strip().upper()
            if not isinstance(registration_proxy, dict) and isinstance(extra.get("registration_proxy"), dict):
                registration_proxy = extra.get("registration_proxy")
        if len(explicit) == 2:
            return explicit
        if isinstance(registration_proxy, dict):
            code = proxy_cfg.node_country_code(str(registration_proxy.get("node_name") or ""))
            if code:
                return code
        return proxy_cfg.node_country_code(str(acc.get("proxy_used") or ""))
    except Exception:
        return ""


def _resolve_live_check_route(proxy: str | None, *, country_hint: str = "") -> dict:
    """查活跟随注册出口策略；Resin 关闭时使用通用 Mihomo 筛选。

    country_hint：新号指定候选国家（固定注册出口），失败自动回退默认筛选。
    """
    from config import proxy as proxy_cfg

    if not bool(getattr(proxy_cfg, "REGISTRATION_PROXY_REQUIRED", False)):
        hint = str(country_hint or "").strip().upper()
        if hint:
            try:
                selection = proxy_cfg.pick_registration_proxy(allowed_countries_override={hint})
            except Exception:
                # 指定国家选不出来就回退默认筛选，不让"出口粘性"变成硬失败
                selection = proxy_cfg.pick_registration_proxy()
        else:
            selection = proxy_cfg.pick_registration_proxy()
        selected = str(selection.get("proxy_url") or "")
        transparent = bool(selection.get("transparent"))
        from core.registration_preflight import preflight_proxy

        preflight = preflight_proxy(
            selected,
            require_country="",
            allowed_countries=selection.get("allowed_countries") or getattr(proxy_cfg, "REGISTRATION_PROXY_ALLOWED_COUNTRIES", []),
            excluded_countries=selection.get("excluded_countries") or getattr(proxy_cfg, "REGISTRATION_PROXY_EXCLUDED_COUNTRIES", ["HK"]),
            allow_transparent=transparent,
            route_identity=str(selection.get("node_name") or selected),
            force=True,
        )
        if not preflight.get("ok"):
            raise RuntimeError(f"查活代理预检失败: {preflight.get('reason') or 'unknown'}")
        selection = dict(selection)
        selection.update({
            "preflight": dict(preflight),
            "exit_country": str(preflight.get("country") or ""),
            "exit_ip": str(preflight.get("ip") or ""),
            "exit_geo": {
                key: preflight.get(key)
                for key in ("ip", "country", "colo")
                if preflight.get(key)
            },
        })
        return {
            "proxy": selected,
            "proxy_mode": (
                "mihomo_excluded_transparent" if transparent and selection.get("mode") == "mihomo_excluded"
                else "mihomo_us_transparent" if transparent
                else "mihomo_excluded" if selection.get("mode") == "mihomo_excluded"
                else "mihomo_us"
            ),
            "network_route": "transparent" if transparent else "proxy",
            "proxy_used": selected or ("router-policy" if transparent else ""),
            "proxy_fallback_reason": None,
            "proxy_group": selection.get("group"),
            "proxy_node": selection.get("node_name"),
            "proxy_selection": selection,
        }
    return resolve_plan_check_route(explicit_proxy=proxy)


def _run_live_check_inner(*, account_id: int, email: str, proxy: str | None, trigger: str, method: str = "") -> dict:
    relay = None
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        country_hint = _young_account_country_hint(account_id)
        try:
            route = _resolve_live_check_route(proxy, country_hint=country_hint)
        except Exception as exc:
            # 协议查活（RT 刷新 / 密码+2FA）只是一次 HTTPS 调用，不依赖出口国家；
            # 预检超时（3s 打 auth.openai.com）不该让整个查活失败。
            if str(method or "").strip().lower() in {"protocol", "protocol_password", "password"}:
                _append_log(email, f"[查活] 路由预检失败，协议查活改用默认出口继续：{str(exc)[:120]}")
                route = {
                    "proxy": None,
                    "proxy_mode": "direct",
                    "network_route": "direct",
                    "proxy_used": "",
                    "proxy_selection": None,
                    "proxy_fallback_reason": f"preflight_failed: {type(exc).__name__}",
                }
            else:
                raise
        selected_proxy = route.get("proxy")
        from config import proxy as proxy_cfg
        timeout = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0) or 15.0)
        effective_proxy, relay = open_plan_check_proxy(
            route, selected_proxy, timeout=timeout,
        )
        # 查活必须沿用账号注册时记录的邮箱来源。不能只调用
        # resolve_email_source(email)：Remail 等临时邮箱的上下文只在领取进程
        # 内存中存在，服务重启后按当前 EMAIL_SOURCE 推断会把来源判错。
        try:
            account = db.get_account(account_id) or {}
        except Exception:
            account = {}
        email_source = str(account.get("email_source") or "").strip() or None
        if email_source:
            _append_log(email, f"[查活] 使用注册时保存的邮箱来源：{email_source}")
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"group={route.get('proxy_group') or '-'} node={route.get('proxy_node') or '-'} "
            f"country_hint={country_hint or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        # 每个网络路由尝试拥有自己的任务级身份状态；同一路由的完整认证链及
        # 内部重试复用同一组 device/session 标识，不同账号绝不共享。
        fingerprint_state: dict = {}
        result = check_account_liveness(
            email,
            proxy=effective_proxy,
            clear_log=False,
            rotate_transparent_route=route.get("network_route") == "transparent",
            proxy_selection=route.get("proxy_selection"),
            email_source=email_source,
            fingerprint_state=fingerprint_state,
            method=method or None,
        )
        # 认证链早期 403 通常是该出口被 CF 拦截，不代表账号死亡。
        # auto/proxy 模式下如果用了代理，额外直连兜底一次，便于和套餐查询的 auto 语义保持接近。
        err_text = str(result.get("error") or "")
        if (
            not result.get("ok")
            and result.get("status") == "failed"
            and "403" in err_text
            and selected_proxy
            and str(route.get("network_route") or "") == "proxy"
        ):
            _append_log(
                email,
                "[查活] 代理路线完整会话收到 403，启动独立直连会话兜底一次（不复用代理画像/Cookie/会话ID）",
            )
            # BrowserSession 约定：None=从代理池抽取，""=明确直连。
            # 出口发生变化时必须重新按真实出口探测画像，不能把代理的 JP/VN
            # 语言时区伪装到直连；因此直连兜底使用独立的任务身份状态。
            result = check_account_liveness(
                email,
                proxy="",
                clear_log=False,
                email_source=email_source,
                fingerprint_state={},
                method=method or None,
            )
        db.update_account_liveness(account_id, result)
        if result.get("ok"):
            _append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
            # 查活顺带刷新套餐/额度：用刚拿到的新 AT 入队一次套餐查询（网络层，不占浏览器）
            try:
                from core.plan_check_service import enqueue_account_plan_check

                plan_queued = enqueue_account_plan_check(
                    account_id=account_id,
                    email=email,
                    access_token=str(result.get("access_token") or ""),
                    trigger="after_liveness",
                    proxy=None,
                    timezone_offset_min="-",
                )
                if plan_queued.get("accepted"):
                    _append_log(email, "[套餐] 查活完成，已入队刷新套餐/额度")
                elif not plan_queued.get("busy"):
                    _append_log(email, f"[套餐] 入队跳过：{plan_queued.get('error') or plan_queued.get('status') or 'unknown'}")
            except Exception as exc:
                _append_log(email, f"[套餐] 入队异常：{type(exc).__name__}")
            try:
                from core.chatgpt2api_push import enqueue_account_push
                pushed = enqueue_account_push(
                    account_id,
                    expected_token_fingerprint=db.token_fingerprint(
                        result.get("access_token") or ""
                    ),
                )
                if pushed.get("accepted"):
                    _append_log(email, "[推送] 测活成功，已进入 chatgpt2api 推送队列")
                elif not pushed.get("disabled"):
                    _append_log(email, f"[推送] 入队跳过：{pushed.get('error') or pushed.get('status') or 'unknown'}")
            except Exception as exc:
                _append_log(email, f"[推送] 入队异常：{type(exc).__name__}")
        elif result.get("status") == "confirmed_dead":
            _append_log(email, f"[查活] 完成：确认死亡 {result.get('error') or ''}")
            # 账号已废 → 同步停用邮箱池条目，便于运营区分"账号仍存活的 used"与"已判废"。
            try:
                from core.email_provider import release_email

                reason = str(result.get("error") or "confirmed_dead")[:160]
                release_email(email, status="disabled", note=f"查活确认账号已废: {reason}")
                _append_log(email, "[查活] 已同步停用邮箱池条目（disabled）")
            except Exception as exc:
                _append_log(email, f"[查活] 邮箱池停用失败：{type(exc).__name__}: {exc}")
        else:
            _append_log(email, f"[查活] 完成：临时错误 {result.get('error') or ''}")
        result.update({
            "network_route": route.get("network_route"),
            "proxy_used": _mask_proxy(selected_proxy) or None,
            "upstream_proxy_used": route.get("upstream_proxy_used"),
            "proxy_mode": route.get("proxy_mode"),
            "proxy_fallback_reason": route.get("proxy_fallback_reason"),
        })
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "temporary_error",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", redact_email(email))
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        if relay is not None:
            relay.close()
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str, method: str = "") -> dict:
    with pipeline_slot("live_check"):
        return _run_live_check_inner(
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=trigger,
            method=method,
        )


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None, method: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger} method={str(method or 'auto')}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
            method=str(method or ""),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "temporary_error",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
