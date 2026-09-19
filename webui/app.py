# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的 SQLite 持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import os
import gzip
import json
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, make_response, render_template, request
import pyotp

from core import codex_retry_service, db, plan_check_service, extract_link_service, codex_agent_service, live_check_service, registration_scheduler, overnight_pipeline
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from core.codex_oauth_policy import evaluate_oauth_eligibility
from webui import config_editor

logger = logging.getLogger(__name__)

_POOL_SOURCE_VALUES = frozenset(("all", "outlook", "generic_api", "imap", "cloudflare_domain", "icloud"))


def _pool_source_arg(default: str = "outlook") -> str:
    src = str(request.args.get("source") or "").strip().lower()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = str(data.get("source") or data.get("type") or "").strip().lower()
    return src if src in _POOL_SOURCE_VALUES else default


def _icloud_mail_client():
    """iCloud 隐藏邮箱池是文件存储（core.icloud_mail_client），不在 SQLite email_pool 表里。"""
    from core import icloud_mail_client

    return icloud_mail_client


def _icloud_pool_summary() -> dict:
    try:
        return dict(_icloud_mail_client().mailbox_summary() or {})
    except Exception:
        logger.exception("读取 iCloud 邮箱池统计失败")
        return {"total": 0, "available": 0, "in_use": 0, "used": 0, "failed": 0, "disabled": 0}


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _client_accepts_gzip(accept_encoding: str | None) -> bool:
    """按 HTTP Accept-Encoding 的 q 值判断客户端是否明确接受 gzip。"""
    for item in str(accept_encoding or "").split(","):
        parts = [part.strip() for part in item.split(";")]
        encoding = parts[0].lower()
        if encoding not in {"gzip", "*"}:
            continue
        quality = 1.0
        for param in parts[1:]:
            key, separator, value = param.partition("=")
            if key.strip().lower() != "q" or not separator:
                continue
            try:
                quality = float(value.strip())
            except ValueError:
                quality = 0.0
            break
        return quality > 0
    return False


def _maybe_compress_json_response(response: Response, accept_encoding: str | None) -> Response:
    """压缩较大的 JSON 响应，且不改变未声明 gzip 能力的客户端语义。"""
    if (
        response.direct_passthrough
        or response.headers.get("Content-Encoding")
        or not _client_accepts_gzip(accept_encoding)
        or (response.mimetype or "").lower() != "application/json"
    ):
        return response
    data = response.get_data()
    if len(data) < 1024:
        return response
    compressed = gzip.compress(data, compresslevel=6, mtime=0)
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    vary = response.headers.get("Vary")
    response.headers["Vary"] = "Accept-Encoding" if not vary else f"{vary}, Accept-Encoding"
    return response

def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
        "oauth_eligibility": evaluate_oauth_eligibility(row),
    }

    extra_raw = row.get("extra_json")
    extra = {}
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except Exception:
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    password = str(
        extra.get("registration_password")
        or row.get("registration_password")
        or row.get("password")
        or ""
    ).strip()
    if password:
        out["password"] = password

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "original_email", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "plan_check_status", "codex_status", "codex_agent_status",
        "totp_setup_status",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        "live_check_proxy_used", "live_check_fingerprint_text",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
        "totp_setup_error", "totp_setup_message", "totp_setup_started_at", "totp_setup_completed_at",
        "email_change_status", "email_change_error", "email_change_new_email",
        "email_change_started_at", "email_change_completed_at",
        # 生图额度（套餐查询副产物）。
        "image_quota", "image_quota_reset_at", "image_quota_unknown",
        "image_quota_checked_at", "image_quota_error",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "copy_line":
        try:
            from core.db import _account_line

            return str(_account_line(row) or "")
        except Exception:
            return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "totp_secret":
        return str(row.get("totp_secret") or "")
    if field == "totp_code":
        secret = str(row.get("totp_secret") or "").strip()
        return pyotp.TOTP(secret).now() if secret else ""
    if field == "password":
        extra_raw = row.get("extra_json")
        extra = {}
        if isinstance(extra_raw, str) and extra_raw.strip():
            try:
                extra = json.loads(extra_raw)
            except Exception:
                extra = {}
        elif isinstance(extra_raw, dict):
            extra = extra_raw
        return str(extra.get("registration_password") or row.get("registration_password") or "未设置")
    raise ValueError("field 仅支持 access_token/copy_line/codex_agent_token/totp_secret/totp_code/password")


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    traffic = row.get("network_traffic")
    if isinstance(traffic, dict) and traffic.get("available"):
        # 流量统计只包含字节计数，不带 URL/Header/请求体，可直接随任务列表返回。
        out["network_traffic"] = traffic
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts


def _read_log_tail(path, *, max_bytes: int, default_running: bool = False, running_fn=None) -> dict:
    if not path.exists():
        return {"ok": True, "log": "", "running": bool(default_running)}
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        content = f.read().decode("utf-8", errors="replace")
    running = bool(default_running)
    if callable(running_fn):
        try:
            running = bool(running_fn())
        except Exception:
            pass
    return {"ok": True, "log": content, "running": running}

def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    _prepared_downloads: dict[str, dict] = {}
    _prepared_downloads_lock = threading.Lock()
    _prepared_download_ttl_seconds = 600

    @app.after_request
    def _compress_json_response(response: Response):
        """默认对 JSON API 响应启用 gzip，减少本地前端拉取大列表的传输体积。"""
        return _maybe_compress_json_response(response, request.headers.get("Accept-Encoding"))

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理过期下载，避免账号凭据长期留在进程内存中。
        download_id = uuid.uuid4().hex
        with _prepared_downloads_lock:
            for k, v in list(_prepared_downloads.items()):
                if now - float(v.get("created_at") or 0) > _prepared_download_ttl_seconds:
                    _prepared_downloads.pop(k, None)
            _prepared_downloads[download_id] = {
                "content": bytes(content),
                "filename": filename,
                "mimetype": mimetype,
                "created_at": now,
            }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        now = time.time()
        key = str(download_id or "")
        with _prepared_downloads_lock:
            # 浏览器可能对同一个下载 URL 发起 HEAD/GET/重试请求；在短 TTL
            # 内保持幂等读取，避免首个 GET 成功后后续重试拿到 404。
            for stale_key, value in list(_prepared_downloads.items()):
                if now - float(value.get("created_at") or 0) > _prepared_download_ttl_seconds:
                    _prepared_downloads.pop(stale_key, None)
            item = _prepared_downloads.get(key)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)
    try:
        from core.icloud_mail_client import _mailboxes_path as _icloud_mailboxes_path
        from core.icloud_mail_client import sync_registered_mailboxes as _sync_icloud_mailboxes

        if _icloud_mailboxes_path().is_file():
            _icloud_sync = _sync_icloud_mailboxes(db.list_accounts(limit=1_000_000, archived="all"))
            if _icloud_sync.get("accounts"):
                logger.info("已同步注册账号到 iCloud 邮箱池: %s", _icloud_sync)
        else:
            logger.info("iCloud 邮箱池文件不存在，跳过启动回填")
    except Exception:
        logger.exception("启动时同步注册账号到 iCloud 邮箱池失败")
    if str(os.environ.get("TURB_WEBUI_BOOT", "")).strip() == "1":
        recovered_registration_jobs = db.recover_interrupted_registration_jobs()
        if recovered_registration_jobs:
            logger.warning("已回收 %s 个因 WebUI 重启中断的注册任务状态", recovered_registration_jobs)
        recovered_pushes = db.recover_interrupted_account_pushes()
        if recovered_pushes:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的推送任务", recovered_pushes)
        recovered_plan_checks = db.recover_interrupted_plan_checks()
        if recovered_plan_checks:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
        recovered_extract_links = db.recover_interrupted_extract_links()
        if recovered_extract_links:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
        recovered_live_checks = db.recover_interrupted_live_checks()
        if recovered_live_checks:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
        recovered_codex_agents = db.recover_interrupted_codex_agents()
        if recovered_codex_agents:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)
        recovered_totp_setups = db.recover_interrupted_totp_setups()
        if recovered_totp_setups:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的 2FA 状态", recovered_totp_setups)
        recovered_email_changes = db.recover_interrupted_email_changes()
        if recovered_email_changes:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的邮箱换绑状态", recovered_email_changes)


    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        requested_ui = (request.args.get("ui") or "").strip().lower()
        if requested_ui in {"legacy", "modern"}:
            ui_mode = requested_ui
        else:
            ui_mode = (request.cookies.get("ui_mode") or "modern").strip().lower()
            if ui_mode not in {"legacy", "modern"}:
                ui_mode = "modern"

        template_name = "index_legacy.html" if ui_mode == "legacy" else "index.html"
        resp = make_response(render_template(template_name))
        if requested_ui in {"legacy", "modern"}:
            resp.set_cookie("ui_mode", ui_mode, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # GPTMail/MailNest/CloudMail 地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.imap_email_pool_summary() if src == "imap"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else _icloud_pool_summary() if src == "icloud"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        codex_filter = str(request.args.get("codex_status", default="") or "").strip().lower()
        totp_filter = str(
            request.args.get("totp_status")
            or request.args.get("totp_filter")
            or request.args.get("twofa_status")
            or ""
        ).strip().lower()
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter)
            result["items"] = [_compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        return jsonify(db.list_accounts(limit=limit, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter))

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        codex_filter = str(request.args.get("codex_status", default="") or "").strip().lower()
        totp_filter = str(
            request.args.get("totp_status")
            or request.args.get("totp_filter")
            or request.args.get("twofa_status")
            or ""
        ).strip().lower()
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter)
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(limit=max(1, min(5000, limit)), archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter)
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = _account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/<int:acc_id>/totp-setup")
    def api_account_totp_setup(acc_id: int):
        """为单个账号开启 2FA/TOTP，成功后自动把 secret 写回账号记录。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        if bool(acc.get("totp_secret")):
            return jsonify({"ok": False, "error": "该账号已经开启 2FA"}), 400

        try:
            from core import twofa_service
        except Exception as exc:
            return jsonify({"ok": False, "error": f"2FA 服务加载失败：{type(exc).__name__}: {exc}"}), 503

        queued = twofa_service.enqueue_account_totp_setup(
            account_id=acc_id,
            email=str(acc.get("email") or ""),
            access_token=token,
            trigger="manual",
            proxy=str(acc.get("proxy_used") or "") or None,
        )
        queued_payload = {k: v for k, v in queued.items() if k != "future"}
        if queued.get("busy"):
            return jsonify({"ok": False, **queued_payload}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued_payload}), 503
        return jsonify({
            "ok": True,
            "started": True,
            "queue": twofa_service.queue_settings(),
            **queued_payload,
        }), 202

    @app.post("/api/accounts/<int:acc_id>/change-email")
    def api_account_change_email(acc_id: int):
        """给单个账号排队换绑邮箱。Body {source}."""
        data = request.get_json(silent=True) or {}
        source = str(data.get("source") or "").strip().lower()
        allowed = {"outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail"}
        if source not in allowed:
            return jsonify({"ok": False, "error": "请选择有效的邮箱来源"}), 400
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not str(acc.get("access_token") or "").strip():
            return jsonify({"ok": False, "error": "账号缺少 access_token，请先查活刷新 AT"}), 400
        from core import email_change_service
        result = email_change_service.enqueue(acc_id, source, trigger="manual")
        public = {k: v for k, v in result.items() if k != "future"}
        return jsonify({"ok": bool(result.get("accepted")), **public}), (202 if result.get("accepted") else 409)

    @app.post("/api/accounts/change-email-bulk")
    def api_accounts_change_email_bulk():
        """批量换绑邮箱。Body {account_ids:[...], source}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        source = str(data.get("source") or "").strip().lower()
        allowed = {"outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail"}
        if source not in allowed:
            return jsonify({"ok": False, "error": "请选择有效的邮箱来源"}), 400
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400
        from core import email_change_service
        started, skipped = [], []
        seen_ids: set[int] = set()
        for raw_id in ids:
            try:
                acc_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not str(acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            result = email_change_service.enqueue(acc_id, source, trigger="manual_bulk")
            if result.get("accepted"):
                started.append({"id": acc_id, "email": acc.get("email"), "status": "queued"})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": result.get("error")})
        return jsonify({"ok": True, "started": started, "started_count": len(started), "skipped": skipped}), 202

    @app.post("/api/accounts/totp-setup-bulk")
    def api_accounts_totp_setup_bulk():
        """批量把账号 2FA/TOTP 设置任务加入后台队列。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            token = str(acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            if str(acc.get("totp_secret") or "").strip():
                skipped.append({"id": acc_id, "email": email, "reason": "该账号已经开启 2FA"})
                continue
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        try:
            from core import twofa_service
        except Exception as exc:
            return jsonify({"ok": False, "error": f"2FA 服务加载失败：{type(exc).__name__}: {exc}"}), 503

        started = []
        busy = []
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "").strip()
            try:
                queued = twofa_service.enqueue_account_totp_setup(
                    account_id=acc_id,
                    email=email,
                    access_token=str(acc.get("access_token") or "").strip(),
                    trigger="manual_bulk",
                    proxy=str(acc.get("proxy_used") or "") or None,
                )
            except Exception as exc:
                failed.append({
                    "id": acc_id,
                    "email": email,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

            # Future 对象不可 JSON 序列化；批量接口只返回队列结果摘要。
            public_result = {k: v for k, v in queued.items() if k != "future"}
            item = {"id": acc_id, "email": email, **public_result}
            if queued.get("accepted"):
                item["status"] = "queued"
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个 2FA 设置任务",
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "queue": twofa_service.queue_settings(),
        }), 202

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/confirmed-dead.txt")
    def api_confirmed_dead_accounts_txt():
        """导出确认死亡账号邮箱；每行一个邮箱，不包含 token。"""
        rows = db.list_accounts(
            limit=1_000_000,
            archived="all",
            status_filter="confirmed_dead",
        )
        emails = [str(row.get("email") or "").strip() for row in rows]
        content = "\n".join(email for email in emails if email)
        if content:
            content += "\n"
        return Response(
            content,
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="confirmed-dead-emails.txt"',
                "Cache-Control": "no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        method = str(data.get("method") or "").strip().lower()
        if method not in {"protocol", "classic"}:
            method = ""
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
                method=method or None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "method": method or "auto",
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数。"""
        code = (request.args.get("code") or "").strip() or None
        try:
            return jsonify({"ok": True, **extract_link_service.query_cdk(cdk=code)})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    def _is_extract_eligible(acc: dict) -> bool:
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        return plan == "free" and bool(acc.get("plus_trial_eligible"))

    @app.post("/api/accounts/extract-link")
    def api_account_extract_link():
        """单账号提链。Body {account_id|id, link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = extract_link_service.enqueue_account_extract(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                link_type=data.get("link_type"),
                cdk=data.get("cdk"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    def api_accounts_extract_link_bulk():
        """批量提链。Body {account_ids:[...], link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = extract_link_service.enqueue_account_extract(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    link_type=data.get("link_type"),
                    cdk=data.get("cdk"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """从 SQLite 返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        stored = db.get_codex_agent_credential(int(acc.get("id") or 0))
        if stored:
            return stored

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-oauth-bulk")
    def api_accounts_download_oauth_bulk():
        """导出选中账号本地已持久化的完整 ChatGPT OAuth 凭据。"""
        import io
        import json as _json
        import re
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多导出 1000 个账号"}), 400

        def first(*values) -> str:
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        def safe_name(email: str, account_id: int) -> str:
            value = re.sub(r"[^A-Za-z0-9._-]+", "_", email).strip("._")
            return value or f"account-{account_id}"

        errors: list[dict] = []
        added: list[dict] = []
        used_names: set[str] = set()
        seen_ids: set[int] = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    account_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if account_id in seen_ids:
                    continue
                seen_ids.add(account_id)
                account = db.get_account(account_id)
                if not account:
                    errors.append({"id": account_id, "error": "账号不存在"})
                    continue
                email = str(account.get("email") or "").strip()
                access_token = first(account.get("chatgpt_oauth_access_token"), account.get("access_token"))
                # Outlook 的顶层 refresh_token 属于邮箱池（用于读取 OTP），
                # 不能在缺少 chatgpt_refresh_token 时被误导出为 ChatGPT OAuth 凭据。
                chatgpt_refresh_token = first(account.get("chatgpt_refresh_token"))
                email_source = str(account.get("email_source") or "").strip().lower()
                mailbox_sources = {
                    "outlook", "generic_api", "cloudmail", "mailnest",
                    "cloudflare", "cloudflare_domain",
                }
                refresh_token = chatgpt_refresh_token
                if not refresh_token and email_source not in mailbox_sources:
                    # 兼容早期 iCloud/无来源记录把 ChatGPT OAuth refresh_token
                    # 写在顶层字段的旧格式。
                    refresh_token = first(account.get("refresh_token"))
                id_token = first(account.get("chatgpt_id_token"), account.get("id_token"))
                if not email or not access_token or not refresh_token or not id_token:
                    errors.append({
                        "id": account_id,
                        "email": email,
                        "error": "缺少完整 OAuth 凭据（email/access_token/refresh_token/id_token）",
                    })
                    continue

                credential = {
                    "type": "codex",
                    "email": email,
                    "expired": first(account.get("chatgpt_token_expires_at"), account.get("token_expires_at"), account.get("expires_at")),
                    "id_token": id_token,
                    "account_id": first(account.get("chatgpt_account_id"), account.get("account_id")),
                    "disabled": bool(
                        account.get("archived")
                        or str(account.get("codex_status") or "").lower() in {"deactivated", "disabled"}
                        or str(account.get("live_check_status") or "").lower() in {"confirmed_dead", "deactivated"}
                    ),
                    "access_token": access_token,
                    "session_token": first(account.get("session_token")),
                    "last_refresh": first(account.get("last_refresh"), account.get("chatgpt_credential_updated_at")),
                    "refresh_token": refresh_token,
                    "oauth_client_id": first(account.get("chatgpt_oauth_client_id"), account.get("oauth_client_id")),
                    "oauth_status": str(account.get("oauth_status") or "success"),
                    "oauth_completed_at": first(account.get("oauth_completed_at")),
                }
                arcname = f"codex-{safe_name(email, account_id)}-oauth.json"
                if arcname in used_names:
                    arcname = f"codex-{safe_name(email, account_id)}-{account_id}-oauth.json"
                used_names.add(arcname)
                zf.writestr(arcname, _json.dumps(credential, ensure_ascii=False, indent=2) + "\n")
                added.append({"id": account_id, "email": email, "filename": arcname})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "local_chatgpt_oauth",
                "format": "codex_oauth_json",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可导出的完整 OAuth 凭据", "errors": errors}), 404
        filename = f"accounts-oauth-{_dt.now().strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, filename, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": filename,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/import-oauth")
    def api_accounts_import_oauth():
        """导入完整 OAuth JSON 或 OAuth ZIP；按邮箱/账号 ID覆盖本地账号。"""
        import base64
        import io
        import json as _json
        import zipfile

        def extract(value) -> list[dict]:
            if isinstance(value, list):
                rows: list[dict] = []
                for item in value:
                    rows.extend(extract(item))
                return rows
            if isinstance(value, str):
                # sub2api 的 codex-session 导入把每条凭据塞成 JSON 字符串
                text = value.strip()
                if text.startswith("{") or text.startswith("["):
                    try:
                        return extract(_json.loads(text))
                    except Exception:
                        return []
                return []
            if not isinstance(value, dict):
                return []
            # 兼容不同导出器的顶层包装：credentials/accounts/items/records/data。
            # 只有看起来像凭据的对象才作为记录，避免把 manifest 当账号导入。
            credential_keys = {
                "email", "access_token", "accessToken", "refresh_token", "refreshToken",
                "id_token", "idToken", "account_id", "chatgpt_account_id",
            }
            if credential_keys.intersection(value):
                return [value]
            for key in (
                "credentials", "accounts", "items", "records", "data", "contents",
                "credential", "auth_json", "authJson", "auth", "auth_file", "authFile", "file",
            ):
                nested = value.get(key)
                if isinstance(nested, (list, dict)):
                    rows = extract(nested)
                    if rows:
                        return rows
            return []

        records: list[dict] = []
        uploaded = request.files.get("file")
        if uploaded is not None:
            raw_bytes = uploaded.read()
            if zipfile.is_zipfile(io.BytesIO(raw_bytes)):
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
                    for info in archive.infolist():
                        if info.is_dir() or not info.filename.lower().endswith(".json"):
                            continue
                        try:
                            records.extend(extract(_json.loads(archive.read(info).decode("utf-8"))))
                        except Exception:
                            continue
            else:
                try:
                    records = extract(_json.loads(raw_bytes.decode("utf-8")))
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"OAuth JSON 解析失败: {type(exc).__name__}"}), 400
        else:
            data = request.get_json(silent=True) or {}
            if isinstance(data, dict) and data.get("base64"):
                try:
                    decoded = base64.b64decode(str(data["base64"]))
                    records = extract(_json.loads(decoded.decode("utf-8")))
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"base64 OAuth 文件解析失败: {type(exc).__name__}"}), 400
            else:
                # JSON 既可以是单条凭据，也可以是 {accounts/items/credentials: [...]} 包装。
                records = extract(data)

        if not records:
            return jsonify({"ok": False, "error": "没有解析到 OAuth 凭据"}), 400
        result = db.import_account_credentials(records, source=(request.form.get("source") if uploaded else None))
        return jsonify({"ok": True, **result})

    @app.post("/api/accounts/export-json-bulk")
    def api_accounts_export_json_bulk():
        """导出**单个完整 JSON**（邮箱 + 密码 + 2FA + OAuth 凭据），不再打包 ZIP。

        结构：{"format": "multi_account_v1", "accounts": [...]}
        每个账号同时兼容 sub2api / CPA 的 codex 凭据字段，下游可直接按邮箱取用；
        也要能被本机 /api/accounts/import-oauth 原样导回（含密码与 2FA）。
        """
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 2000:
            return jsonify({"ok": False, "error": "单次最多导出 2000 个账号"}), 400

        def first(*values) -> str:
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        accounts: list[dict] = []
        errors: list[dict] = []
        exported_ids: list[int] = []
        seen: set[int] = set()
        for raw_id in ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                errors.append({"id": raw_id, "error": "ID 非法"})
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            account = db.get_account(account_id)
            if not account:
                errors.append({"id": account_id, "error": "账号不存在"})
                continue
            email = first(account.get("email"))
            if not email:
                errors.append({"id": account_id, "error": "缺少 email"})
                continue
            access_token = first(
                account.get("chatgpt_oauth_access_token"), account.get("access_token")
            )
            refresh_token = first(account.get("chatgpt_refresh_token"))
            id_token = first(account.get("chatgpt_id_token"), account.get("id_token"))
            complete = bool(access_token and refresh_token and id_token)
            exported_ids.append(account_id)
            accounts.append({
                # ---- sub2api / CPA 兼容字段 ----
                "type": "codex" if complete else "account_migration",
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expired": first(
                    account.get("chatgpt_token_expires_at"),
                    account.get("token_expires_at"),
                    account.get("expires_at"),
                ),
                "account_id": first(
                    account.get("chatgpt_account_id"), account.get("account_id")
                ),
                "oauth_client_id": first(
                    account.get("chatgpt_oauth_client_id"), account.get("oauth_client_id")
                ),
                "session_token": first(account.get("session_token")),
                "disabled": bool(
                    account.get("archived")
                    or str(account.get("codex_status") or "").lower() in {"deactivated", "disabled"}
                    or str(account.get("live_check_status") or "").lower()
                    in {"confirmed_dead", "deactivated"}
                ),
                # ---- 本机扩展字段（导入时原样写回）----
                "credential_kind": "complete" if complete else "access_only",
                "password": first(account.get("password")),
                "totp_secret": first(account.get("totp_secret")),
                "plan_type": first(account.get("plan_type"), account.get("current_plan_type")),
                "email_source": first(account.get("email_source")),
                "live_check_status": first(account.get("live_check_status")),
                "note": str(account.get("note") or ""),
            })

        if not accounts:
            return jsonify({"ok": False, "error": "没有可导出的账号", "errors": errors}), 404

        # 标记已导出：UI 上显示“已导出”徽章并累计次数（避免重复导出/重复推送给下游）。
        if exported_ids:
            try:
                db.mark_accounts_exported(exported_ids)
            except Exception as exc:
                logger.warning("[export-json] 标记已导出失败: %s", str(exc)[:160])

        fmt = str(data.get("format") or "multi_account_v1").strip().lower()
        ts = _dt.now().strftime("%Y%m%d-%H%M%S")
        exported_at = _dt.now().isoformat(timespec="seconds")
        mimetype = "application/json"

        if fmt in {"access_token", "at", "access-token", "pure_at"}:
            # 纯 AT 格式：每行 email----access_token，下游最常见、最省事的形态
            lines = [f"{a['email']}----{a['access_token']}" for a in accounts if a.get("access_token")]
            body = "\n".join(lines) + "\n"
            filename = f"accounts-at-{ts}.txt"
            mimetype = "text/plain; charset=utf-8"
        elif fmt in {"sub2api", "sub2"}:
            # sub2api accounts[] 形态：{"accounts":[{name,platform,type,credentials,extra}],"proxies":[]}
            entries = []
            for a in accounts:
                entries.append({
                    "name": a["email"],
                    "platform": "openai",
                    "type": "codex",
                    "credentials": {
                        "email": a["email"],
                        "access_token": a["access_token"],
                        "refresh_token": a["refresh_token"],
                        "id_token": a["id_token"],
                        "account_id": a["account_id"],
                        "chatgpt_account_id": a["account_id"],
                        "plan_type": a["plan_type"] or "free",
                        "expired": a["expired"],
                        "oauth_client_id": a["oauth_client_id"],
                        "session_token": a["session_token"],
                    },
                    "extra": {
                        "email": a["email"],
                        "account_id": a["account_id"],
                        "source": "register_manager",
                        "live_check_status": a.get("live_check_status") or "",
                    },
                })
            payload = {
                "exported_at": exported_at,
                "source": "register_manager",
                "format": "sub2api_accounts",
                "count": len(entries),
                "proxies": [],
                "errors": errors,
                "accounts": entries,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            filename = f"sub2api-accounts-{ts}.json"
        elif fmt in {"cpa", "codex", "cpajson"}:
            # CPA / Codex CLI 形态：单个 codex 凭据对象数组（等同 codex-*.json 的合集）
            entries = []
            for a in accounts:
                entries.append({
                    "type": "codex",
                    "email": a["email"],
                    "access_token": a["access_token"],
                    "refresh_token": a["refresh_token"],
                    "id_token": a["id_token"],
                    "expired": a["expired"],
                    "account_id": a["account_id"],
                    "disabled": bool(a.get("disabled")),
                    "last_refresh": exported_at,
                    "oauth_client_id": a["oauth_client_id"],
                    "oauth_status": "success" if a.get("refresh_token") else "access_only",
                    "session_token": a["session_token"],
                })
            payload = {
                "exported_at": exported_at,
                "source": "register_manager",
                "format": "cpa_codex",
                "count": len(entries),
                "errors": errors,
                "accounts": entries,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            filename = f"cpa-codex-{ts}.json"
        else:
            payload = {
                "exported_at": exported_at,
                "source": "register_manager",
                "format": "multi_account_v1",
                "count": len(accounts),
                "with_password": sum(1 for a in accounts if a.get("password")),
                "with_twofa": sum(1 for a in accounts if a.get("totp_secret")),
                "with_refresh_token": sum(1 for a in accounts if a.get("refresh_token")),
                "errors": errors,
                "accounts": accounts,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            filename = f"accounts-export-{ts}.json"
        if data.get("prepare"):
            download_id = _put_prepared_download(body.encode("utf-8"), filename, mimetype)
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": filename,
                "format": fmt,
                "added_count": len(accounts),
                "error_count": len(errors),
            })
        return Response(
            body,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/accounts/download-credentials-bulk")
    def api_accounts_download_credentials_bulk():
        """导出账号迁移包；完整 OAuth 和只有 access_token 的账号均可导出。"""
        import io
        import json as _json
        import re
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        added, errors, names = [], [], set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    account_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                account = db.get_account(account_id)
                if not account:
                    errors.append({"id": account_id, "error": "账号不存在"})
                    continue
                email = str(account.get("email") or "").strip()
                access_token = str(account.get("chatgpt_oauth_access_token") or account.get("access_token") or "").strip()
                if not email or not access_token:
                    errors.append({"id": account_id, "email": email, "error": "缺少 email 或 access_token"})
                    continue
                refresh_token = str(account.get("chatgpt_refresh_token") or "").strip()
                id_token = str(account.get("chatgpt_id_token") or account.get("id_token") or "").strip()
                kind = "complete" if refresh_token and id_token else "access_only"
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", email).strip("._") or f"account-{account_id}"
                name = f"account-{safe}-credentials.json"
                if name in names:
                    name = f"account-{safe}-{account_id}-credentials.json"
                names.add(name)
                payload = {
                    "type": "codex" if kind == "complete" else "account_migration",
                    "credential_kind": kind,
                    "email": email,
                    "email_source": account.get("email_source") or "",
                    "account_id": account.get("chatgpt_account_id") or account.get("account_id") or "",
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "id_token": id_token,
                    "oauth_client_id": account.get("chatgpt_oauth_client_id") or account.get("oauth_client_id") or "",
                    "oauth_status": account.get("oauth_status") or "",
                    "email_pool_status": "used",
                }
                zf.writestr(name, _json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                added.append({"id": account_id, "email": email, "filename": name, "credential_kind": kind})
            zf.writestr("manifest.json", _json.dumps({
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "register_account_migration",
                "count": len(added),
                "files": added,
                "errors": errors,
            }, ensure_ascii=False, indent=2) + "\n")
        if not added:
            return jsonify({"ok": False, "error": "没有可导出的账号凭据", "errors": errors}), 404
        filename = f"accounts-credentials-{_dt.now().strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        content = buf.getvalue()
        if data.get("prepare"):
            download_id = _put_prepared_download(content, filename, "application/zip")
            return jsonify({"ok": True, "prepared": True, "download_url": f"/api/downloads/{download_id}", "filename": filename, "added_count": len(added), "error_count": len(errors)})
        return Response(content, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if source == "icloud":
            fetch_all = bool(paged or q or page_arg is not None or page_size_arg is not None)
            rows = _with_pool_source(
                _icloud_mail_client().list_mailboxes(
                    status=status, limit=1_000_000 if fetch_all else max(1, int(limit or 1))
                ),
                "icloud",
            )
            if q:
                ql = q.lower()
                rows = [r for r in rows if ql in str(r.get("email") or "").lower() or ql in str(r.get("label") or "").lower() or ql in str(r.get("note") or "").lower()]
            if fetch_all:
                page = max(1, int(page_arg or 1))
                page_size = max(1, min(500, int(page_size_arg or limit or 50)))
                start = (page - 1) * page_size
                return jsonify({"ok": True, "items": rows[start:start + page_size], "total": len(rows), "page": page, "page_size": page_size})
            return jsonify(rows[: max(1, int(limit or 1))])
        if source == "all" and (paged or q or page_arg is not None or page_size_arg is not None):
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            db_page = db.list_email_pool_page(source="all", status=status, q=q, limit=1_000_000, offset=0)
            rows = list(db_page.get("items") or [])
            rows += _with_pool_source(_icloud_mail_client().list_mailboxes(status=status, limit=1_000_000), "icloud")
            if q:
                ql = q.lower()
                rows = [r for r in rows if ql in str(r.get("email") or "").lower() or ql in str(r.get("label") or "").lower() or ql in str(r.get("note") or "").lower()]
            rows = sorted(rows, key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""), reverse=True)
            start = (page - 1) * page_size
            return jsonify({"ok": True, "items": rows[start:start + page_size], "total": len(rows), "page": page, "page_size": page_size})
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_email_pool_page(
                source=source, status=status, q=q, limit=page_size, offset=offset
            )
            result.update({"ok": True, "page": page, "page_size": page_size})
            return jsonify(result)
        # 兼容旧接口仍返回数组，但查询本身也只从 SQLite 读取 limit 条。
        result = db.list_email_pool_page(
            source=source, status=status, q=q, limit=max(1, int(limit or 1)), offset=0
        )
        return jsonify(result["items"])

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：email----code_url
        通用 IMAP：email----password 或 email:password；服务器/端口/SSL 单独传入
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip()
        if source not in ("outlook", "generic_api", "imap", "icloud"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook、通用 API、通用 IMAP 或 iCloud 隐藏邮箱池"}), 400
        text = data.get("text") or ""
        as_registered = bool(data.get("as_registered", False))
        overwrite = bool(data.get("overwrite", False))
        if source == "icloud":
            result = _icloud_mail_client().import_mailboxes(text, overwrite=overwrite)
            if not result.get("parsed"):
                return jsonify({"ok": False, "error": "未解析到有效 iCloud 隐藏邮箱；每行填写 alias@icloud.com 或 alias@icloud.com----标签"}), 400
            return jsonify(result)
        imap_server = str(data.get("imap_server") or "").strip()
        try:
            imap_port = int(data.get("imap_port") or 993)
        except (TypeError, ValueError):
            imap_port = 0
        imap_ssl_raw = data.get("imap_ssl", True)
        imap_ssl = imap_ssl_raw if isinstance(imap_ssl_raw, bool) else str(imap_ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
        if source == "imap" and (not imap_server or not (1 <= imap_port <= 65535)):
            return jsonify({"ok": False, "error": "通用 IMAP 导入必须填写有效的服务器和端口"}), 400
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if source == "imap":
                if "----" in line:
                    parts = line.split("----", 1)
                elif "====" in line:
                    parts = line.split("====", 1)
                elif ":" in line:
                    parts = line.split(":", 1)
                else:
                    continue
            else:
                parts = line.split("----") if "----" in line else line.split("====")
            parts = [p.strip() for p in parts]
            if source == "generic_api":
                if len(parts) < 2:
                    continue
                records.append({
                    "email": parts[0],
                    "code_url": parts[1],
                    "access_token": parts[2] if len(parts) > 2 else "",
                    "totp_secret": parts[3] if len(parts) > 3 else "",
                })
                continue
            if source == "imap":
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                records.append({
                    "email": parts[0], "imap_password": parts[1],
                    "imap_server": imap_server, "imap_port": imap_port,
                    "imap_ssl": imap_ssl, "imap_username": "",
                })
                continue
            if len(parts) < 4:
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "access_token": parts[4] if len(parts) > 4 else "",
                "totp_secret": parts[5] if len(parts) > 5 else "",
            })
        if not records:
            need = ("2 段：邮箱----取码地址" if source == "generic_api" else
                    "邮箱----IMAP密码 或 邮箱:IMAP密码" if source == "imap" else
                    "4 段：email----password----clientId----refreshToken")
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source == "generic_api":
            inserted, skipped = db.import_generic_api_emails(records, overwrite=overwrite)
        elif source == "imap":
            inserted, skipped = db.import_imap_emails(records, overwrite=overwrite)
        else:
            inserted, skipped = db.import_outlook_accounts(records, overwrite=overwrite)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "as_registered": as_registered,
            "overwrite": overwrite,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "generic_api":
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "imap":
            db.release_imap_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        elif source == "icloud":
            _icloud_mail_client().set_mailbox_status(email, status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "generic_api":
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "imap":
                    db.release_imap_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                elif item_source == "icloud":
                    _icloud_mail_client().set_mailbox_status(email, status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email, source?}。"""
        data = request.get_json(silent=True) or {}
        email = str(data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        raw_source = data.get("source") or data.get("type")
        source = (
            _pool_source_arg()
            if not str(raw_source or "").strip()
            else str(raw_source).strip().lower()
        )
        if source not in _POOL_SOURCE_VALUES:
            return jsonify({"ok": False, "error": "邮箱来源非法"}), 400
        if source == "icloud":
            deleted = _icloud_mail_client().delete_mailbox(email)
        else:
            deleted = db.delete_email_pool(email, source=source)
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {items/emails: [...], source?}。"""
        data = request.get_json(silent=True) or {}
        raw_source = data.get("source") or data.get("type")
        source = (
            _pool_source_arg()
            if not str(raw_source or "").strip()
            else str(raw_source).strip().lower()
        )
        if source not in _POOL_SOURCE_VALUES:
            return jsonify({"ok": False, "error": "邮箱来源非法"}), 400
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[dict] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = str(raw_item.get("email") or "").strip()
                raw_item_source = raw_item.get("source") or raw_item.get("type")
                item_source = (
                    source
                    if not str(raw_item_source or "").strip()
                    else str(raw_item_source).strip().lower()
                )
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if item_source not in _POOL_SOURCE_VALUES:
                skipped.append({"email": email, "source": item_source, "reason": "邮箱来源非法"})
                continue
            key = f"{item_source}:{email.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "icloud":
                    deleted_ok = _icloud_mail_client().delete_mailbox(email)
                else:
                    deleted_ok = db.delete_email_pool(email, source=item_source)
            except Exception as exc:
                skipped.append({
                    "email": email,
                    "source": item_source,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/outlook/sync-registered")
    def api_outlook_sync_registered():
        """按已注册账号回填 iCloud 邮箱池状态；不删除邮箱、不改账号。"""
        try:
            result = _icloud_mail_client().sync_registered_mailboxes(db.list_accounts(limit=1_000_000, archived="all"))
            return jsonify({"ok": True, **result})
        except Exception as exc:
            logger.exception("同步注册账号邮箱池失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        q = str(request.args.get("q", default="") or "").strip()
        archived = str(request.args.get("archived", default="0") or "0").lower()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = db.list_codex_accounts_page(
                archived=archived,
                date_from=date_from,
                date_to=date_to,
                q=q,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            result.update({"ok": True, "page": page, "page_size": page_size})
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        result = db.list_codex_accounts_page(
            archived=archived,
            date_from=date_from,
            date_to=date_to,
            q=q,
            limit=max(1, int(limit or 1)),
            offset=0,
        )
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": result["items"],
        })

    @app.post("/api/codex/archive")
    def api_codex_archive():
        """归档/取消归档一条 Codex 授权凭证。Body {filename, archived}。"""
        data = request.get_json(silent=True) or {}
        filename = str(data.get("filename") or "").strip()
        archived = bool(data.get("archived", True))
        if not filename:
            return jsonify({"ok": False, "error": "filename 必填"}), 400
        try:
            rec = db.archive_codex(filename=filename, archived=archived)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if rec is None:
            return jsonify({"ok": False, "error": f"凭证不存在: {filename}"}), 404
        return jsonify({"ok": True, "filename": filename, "archived": archived, "record": rec})

    @app.post("/api/codex/archive-bulk")
    def api_codex_archive_bulk():
        """批量归档/取消归档 Codex 授权凭证。Body {filenames:[...], archived}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        archived = bool(data.get("archived", True))
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400
        updated = []
        skipped = []
        seen = set()
        for fname in filenames:
            if not isinstance(fname, str) or not fname:
                skipped.append({"filename": str(fname), "reason": "非法文件名"})
                continue
            if fname in seen:
                continue
            seen.add(fname)
            try:
                rec = db.archive_codex(filename=fname, archived=archived)
            except ValueError as exc:
                skipped.append({"filename": fname, "reason": str(exc)})
                continue
            if rec is None:
                skipped.append({"filename": fname, "reason": "凭证不存在"})
            else:
                updated.append({"filename": fname, "archived": archived})
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        eligibility = evaluate_oauth_eligibility(acc)
        if not eligibility.get("eligible"):
            token = str(acc.get("access_token") or "").strip()
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "账号未达到 OAuth 条件且没有 access_token，无法执行轻量查活",
                    "action": "plan_check",
                    "eligibility": eligibility,
                }), 409
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id") or 0),
                email=email,
                access_token=token,
                trigger="oauth_gate",
                proxy=None,
                timezone_offset_min="-",
            )
            return jsonify({
                "ok": True,
                "started": bool(queued.get("accepted")),
                "action": "plan_check",
                "message": "账号尚未满足完整 OAuth 条件，已转为轻量查活",
                "eligibility": eligibility,
                "plan_check": {k: v for k, v in queued.items() if k != "future"},
            }), 202
        if (acc.get("live_check_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        simple_check_started = []
        simple_check_busy = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            eligibility = evaluate_oauth_eligibility(acc)
            if not eligibility.get("eligible"):
                token = str(acc.get("access_token") or "").strip()
                if not token:
                    skipped.append({
                        "id": acc_id,
                        "email": email,
                        "reason": "未达到 OAuth 条件且缺少 access_token",
                        "eligibility": eligibility,
                    })
                    continue
                queued = plan_check_service.enqueue_account_plan_check(
                    account_id=acc_id,
                    email=email,
                    access_token=token,
                    trigger="oauth_gate_bulk",
                    proxy=None,
                    timezone_offset_min="-",
                )
                item = {"id": acc_id, "email": email, "eligibility": eligibility}
                if queued.get("accepted"):
                    simple_check_started.append(item)
                elif queued.get("busy"):
                    simple_check_busy.append(item)
                else:
                    skipped.append({**item, "reason": queued.get("error") or "轻量查活入队失败"})
                continue
            if (acc.get("live_check_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected and not simple_check_started and not simple_check_busy:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        if selected:
            threading.Thread(
                target=_bulk_runner,
                args=(selected, workers, batch_id),
                name=f"codex-bulk-dispatch-{batch_id}",
                daemon=True,
            ).start()
        return jsonify({
            "ok": True,
            "message": f"已补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "simple_check_started": simple_check_started,
            "simple_check_started_count": len(simple_check_started),
            "simple_check_busy": simple_check_busy,
            "simple_check_busy_count": len(simple_check_busy),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        data = _read_log_tail(p, max_bytes=80_000, running_fn=lambda: live_check_service.is_checking(email))
        return jsonify(data)

    @app.get("/api/accounts/totp-setup-log")
    def api_account_totp_setup_log():
        """读取某邮箱最近一次 2FA 设置日志。?email=xxx"""
        from core import twofa_service
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = twofa_service.log_path(email)
        data = _read_log_tail(p, max_bytes=80_000, running_fn=lambda: False)
        try:
            acc = db.get_account_by_email(email) or {}
            data["running"] = bool(str(acc.get("totp_setup_status") or "") in {"queued", "running"}) or twofa_service.is_running(int(acc.get("id") or 0))
        except Exception:
            pass
        return jsonify(data)

    @app.get("/api/accounts/<int:acc_id>/change-email-log")
    def api_account_change_email_log(acc_id: int):
        """读取账号最近一次邮箱换绑日志。"""
        from core import email_change_service
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        data = _read_log_tail(
            email_change_service.log_path(acc_id), max_bytes=80_000,
            running_fn=lambda: email_change_service.is_running(acc_id),
        )
        data["account_id"] = acc_id
        data["email"] = acc.get("email")
        data["running"] = bool(data.get("running") or str(acc.get("email_change_status") or "") in {"queued", "running"})
        return jsonify(data)

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        from config import email as _email_cfg
        manual_otp_required = not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = db.list_jobs_page(
                limit=page_size, offset=(page - 1) * page_size
            )
            rows = result.get("items") or []
            for row in rows:
                row["manual_otp_required"] = manual_otp_required
                row.update(svc.get_retry_info(row))
            result.update({"ok": True, "page": page, "page_size": page_size})
            result["items"] = [_compact_job_for_list(r) for r in rows]
            result["status_counts"] = db.job_status_counts()
            result["compact"] = True
            return jsonify(result)
        rows = db.list_jobs(limit=max(1, int(limit or 1)))
        for row in rows:
            row["manual_otp_required"] = manual_otp_required
            row.update(svc.get_retry_info(row))
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers}。"""
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import parse_email_sources
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            jobs = svc.submit_registration(count=count, workers=workers)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
            })
        sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if "remail" in sources:
            api_base = str(getattr(_email_cfg, "REMAIL_API_BASE", "") or "").strip()
            api_key = str(getattr(_email_cfg, "REMAIL_API_KEY", "") or "").strip()
            try:
                project_id = int(getattr(_email_cfg, "REMAIL_PROJECT_ID", 2) or 0)
            except (TypeError, ValueError):
                project_id = 0
            suffix = str(getattr(_email_cfg, "REMAIL_EMAIL_SUFFIX", "") or "").strip()
            service_mode = str(getattr(_email_cfg, "REMAIL_SERVICE_MODE", "purchase") or "purchase").strip().lower()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if project_id <= 0:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail 项目 ID（配置 → 邮箱 / OTP）。",
                }), 400
            if not suffix:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail 邮箱后缀（例如 outlook.com）。",
                }), 400
            if service_mode not in ("code", "purchase"):
                return jsonify({
                    "ok": False,
                    "error": "Remail 服务模式只能填写 code 或 purchase（配置 → 邮箱 / OTP）。",
                }), 400
        if "icloud" in sources:
            icloud_user = str(getattr(_email_cfg, "ICLOUD_IMAP_USERNAME", "") or "").strip()
            icloud_pass = str(getattr(_email_cfg, "ICLOUD_IMAP_PASSWORD", "") or "").strip()
            if not icloud_user:
                return jsonify({"ok": False, "error": "已选择 iCloud 邮箱来源，请填写 iCloud 主邮箱（配置 → 邮箱 / OTP）。"}), 400
            if not icloud_pass:
                return jsonify({"ok": False, "error": "已选择 iCloud 邮箱来源，请填写 Apple App 专用密码（配置 → 邮箱 / OTP）。"}), 400
        if "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "remail" in sources or "cloudflare" in sources:
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["imap"]:
            pool = db.imap_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 IMAP 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["icloud"]:
            pool = _icloud_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"iCloud 隐藏邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary().get("available", 0)
            if "imap" in sources:
                available += db.imap_email_pool_summary().get("available", 0)
            if "icloud" in sources:
                available += _icloud_pool_summary().get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        jobs = svc.submit_registration(count=count, workers=workers)
        return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "warning": warning, "workers": workers})

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    @app.get("/api/registration/proxy-status")
    def api_registration_proxy_status():
        from core.resin_proxy_status import registration_proxy_status

        return jsonify({"ok": True, **registration_proxy_status(check_tcp=True)})

    @app.post("/api/registration/proxy-test")
    def api_registration_proxy_test():
        from core.resin_proxy_status import test_registration_proxy

        result = test_registration_proxy()
        return jsonify(result), (200 if result.get("ok") else 503)

    @app.get("/api/registration/schedule")
    def api_registration_schedule_get():
        return jsonify({"ok": True, **registration_scheduler.get_schedule()})

    @app.post("/api/registration/schedule")
    def api_registration_schedule_set():
        data = request.get_json(silent=True) or {}
        if data.get("enabled") is False:
            return jsonify({"ok": True, **registration_scheduler.cancel_schedule()})
        try:
            result = registration_scheduler.set_schedule(
                run_at=str(data.get("run_at") or "").strip(),
                count=int(data.get("count", 1)),
                workers=int(data.get("workers", 1)),
                repeat=str(data.get("repeat") or "once"),
                email_source=str(data.get("email_source") or "icloud"),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc) or "定时计划参数无效"}), 400
        return jsonify({"ok": True, **result})

    @app.post("/api/registration/schedule/cancel")
    def api_registration_schedule_cancel():
        return jsonify({"ok": True, **registration_scheduler.cancel_schedule()})

    _pwd_login_state: dict = {
        "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0,
        "current": "", "started_at": None, "finished_at": None, "results": [],
    }
    _pwd_login_lock = threading.Lock()

    def _parse_password_line(raw: str):
        text = str(raw or "").strip()
        if not text or text.startswith("#"):
            return None
        for sep in ("----", "\t", "||", ","):
            if sep in text:
                parts = [p.strip() for p in text.split(sep)]
                if len(parts) >= 2 and parts[0] and parts[1]:
                    return parts[0].lower(), parts[1], (parts[2] if len(parts) > 2 else "")
        return None

    def _pwd_login_worker(items) -> None:
        from core import db as _db
        from core.password_login import login_with_password
        for email, password, totp in items:
            with _pwd_login_lock:
                _pwd_login_state["current"] = email
            entry = {"email": email, "ok": False}
            try:
                acc = _db.get_account_by_email(email) or {}
                country = str(acc.get("proxy_exit_country") or "")
                # ① 先落库：邮箱+密码+2FA 立即入库，协议验证失败也不丢数据（先导入后验证）。
                if acc:
                    _db.update_account_registration_password(email, password)
                    if totp:
                        _db.update_account_totp_secret_by_email(email, totp)
                else:
                    _db.insert_account(
                        email=email,
                        access_token="",
                        totp_secret=totp or None,
                        email_source="import_password",
                        codex_status="skipped",
                        codex_error="导入账号未执行 Codex 授权",
                    )
                    _db.update_account_registration_password(email, password)
                # Codex 授权未跑过的导入账号标为“已跳过”，UI Codex 列有明确状态可筛选。
                current_codex = str((_db.get_account_by_email(email) or {}).get("codex_status") or "").strip()
                if not current_codex:
                    try:
                        _db.update_account_codex_status(email, "skipped", "导入账号未执行 Codex 授权")
                    except Exception:
                        pass
                entry["stored"] = True
                # ② 再协议验证：换取 AT/RT/ID Token 并 CAS 写回。
                res = login_with_password(
                    email, password, totp_secret=totp, country_hint=country,
                    write_back=False, timeout=30,
                )
                if res.get("ok"):
                    credential = {
                        "access_token": res["access_token"],
                        "refresh_token": res["refresh_token"],
                        "id_token": res["id_token"],
                        "oauth_client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                        "source": "password_login",
                        "expires_at": res.get("expires_at"),
                    }
                    current = _db.get_account_by_email(email) or {}
                    old_at = str(current.get("access_token") or current.get("chatgpt_oauth_access_token") or "")
                    entry["write"] = _db.update_account_chatgpt_oauth(email, credential, expected_access_token=old_at)
                    entry["ok"] = bool((entry.get("write") or {}).get("updated"))
                    if not entry["ok"]:
                        entry["error"] = str((entry.get("write") or {}).get("reason") or "凭据写回失败")[:200]
                else:
                    entry["error"] = str(res.get("error") or "")[:200]
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
            with _pwd_login_lock:
                _pwd_login_state["done"] += 1
                _pwd_login_state["ok" if entry["ok"] else "failed"] += 1
                _pwd_login_state["results"].append(entry)
            time.sleep(1)
        with _pwd_login_lock:
            _pwd_login_state["running"] = False
            _pwd_login_state["current"] = ""
            _pwd_login_state["finished_at"] = _pipe_now_iso()

    def _parse_access_token_line(line: str) -> dict | None:
        """识别「email----access_token」这类纯 AT 行；不是就返回 None。"""
        text = str(line or "").strip()
        if not text or text.startswith("#") or "@" not in text:
            return None
        parts = None
        for sep in ("----", "||", "\t", ",", " "):
            if sep in text:
                parts = [p.strip() for p in text.split(sep) if p.strip()]
                break
        if not parts or len(parts) != 2:
            return None
        email, token = parts[0], parts[1]
        if "@" not in email or len(token) < 80:
            return None
        looks_like_token = token.startswith("eyJ") or token.startswith("rt.") or token.count(".") >= 2
        if not looks_like_token:
            return None
        return {"email": email, "access_token": token}

    @app.post("/api/accounts/import-password-login")
    def api_accounts_import_password_login():
        """账密+2FA 导入并自动协议登录验证（纯协议，不启动浏览器）。
        body: {"text": "email----password----totp"} 或 {"accounts": [{"email","password","totp_secret"}]}"""
        data = request.get_json(silent=True) or {}
        items = []
        seen = set()
        raw_accounts = data.get("accounts")
        if isinstance(raw_accounts, list):
            for row in raw_accounts:
                if not isinstance(row, dict):
                    continue
                email = str(row.get("email") or "").strip().lower()
                password = str(row.get("password") or "").strip()
                totp = str(row.get("totp_secret") or row.get("totp") or "").strip()
                if email and password and email not in seen:
                    seen.add(email)
                    items.append((email, password, totp))
        else:
            at_records: list[dict] = []
            for line in str(data.get("text") or "").replace("\r", "\n").split("\n"):
                # 先判纯 AT 行：第二段是长 token（JWT / RT）时不能当密码走协议登录
                at = _parse_access_token_line(line)
                if at:
                    email_key = at["email"].lower()
                    if email_key not in seen:
                        seen.add(email_key)
                        at_records.append(at)
                    continue
                parsed = _parse_password_line(line)
                if parsed:
                    if parsed[0] not in seen:
                        seen.add(parsed[0])
                        items.append(parsed)
                    continue
            if at_records:
                try:
                    from core import db as _db

                    at_result = _db.import_account_credentials(at_records, source="access_token_import")
                    if not items:
                        return jsonify({
                            "ok": True,
                            "queued": 0,
                            "access_token_imported": at_result.get("inserted", 0) + at_result.get("updated", 0),
                            "detail": at_result,
                        })
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"纯 AT 导入失败: {type(exc).__name__}: {exc}"}), 500
        if not items:
            return jsonify({"ok": False, "error": "没有解析到有效账号行（email----password----totp）"}), 400
        with _pwd_login_lock:
            if _pwd_login_state["running"]:
                return jsonify({"ok": False, "error": "协议登录验证任务正在运行，请稍后再试"}), 409
            _pwd_login_state.update({
                "running": True, "total": len(items), "done": 0, "ok": 0, "failed": 0,
                "current": "", "results": [], "finished_at": None,
                "started_at": _pipe_now_iso(),
            })
        threading.Thread(target=_pwd_login_worker, args=(items,), name="password-login-verify", daemon=True).start()
        return jsonify({"ok": True, "queued": len(items)})

    @app.get("/api/accounts/import-password-login/status")
    def api_accounts_import_password_login_status():
        with _pwd_login_lock:
            return jsonify({"ok": True, **_pwd_login_state})

    @app.get("/api/registration/pipeline")
    def api_registration_pipeline_status():
        return jsonify({"ok": True, **overnight_pipeline.get_status()})

    @app.post("/api/registration/pipeline")
    def api_registration_pipeline_action():
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip().lower()
        if action == "start":
            return jsonify({"ok": True, **overnight_pipeline.start()})
        if action == "stop":
            return jsonify({"ok": True, **overnight_pipeline.stop()})
        if action == "resume":
            return jsonify({"ok": True, **overnight_pipeline.resume()})
        return jsonify({"ok": False, "error": "action 仅支持 start / stop / resume"}), 400

    def _pipe_now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    _password_fix_lock = threading.Lock()
    _password_fix_state: dict = {
        "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0, "skipped": 0,
        "current": "", "started_at": None, "finished_at": None, "results": [],
    }

    def _password_fix_worker(items, skip_existing: bool) -> None:
        from core.account_password import set_account_password
        for acc_id, email in items:
            with _password_fix_lock:
                _password_fix_state["current"] = email
            status = "failed"
            detail = ""
            try:
                if skip_existing:
                    acc = db.get_account(acc_id) or {}
                    if str(acc.get("password") or "").strip():
                        status = "skipped"
                if status != "skipped":
                    result = set_account_password(email) or {}
                    if result.get("ok"):
                        status = "ok"
                    else:
                        detail = str(result.get("error") or result.get("status") or "")[:200]
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"[:200]
            with _password_fix_lock:
                _password_fix_state["done"] += 1
                _password_fix_state[status] = int(_password_fix_state.get(status) or 0) + 1
                _password_fix_state["results"].append({
                    "account_id": acc_id, "email": email, "status": status, "error": detail,
                })
        with _password_fix_lock:
            _password_fix_state["running"] = False
            _password_fix_state["current"] = ""
            _password_fix_state["finished_at"] = _pipe_now_iso()

    @app.post("/api/accounts/set-password")
    def api_accounts_set_password():
        """给选中账号补设密码（无头浏览器串行执行）。body {account_ids:[...], skip_existing:true}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        items = []
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                continue
            acc = db.get_account(acc_id) or {}
            email = str(acc.get("email") or "").strip()
            if email:
                items.append((acc_id, email))
        if not items:
            return jsonify({"ok": False, "error": "没有可处理的账号"}), 400
        with _password_fix_lock:
            if _password_fix_state["running"]:
                return jsonify({"ok": False, "error": "补密码任务正在运行，请稍后再试"}), 409
            _password_fix_state.update({
                "running": True, "total": len(items), "done": 0, "ok": 0, "failed": 0,
                "skipped": 0, "current": "", "results": [], "finished_at": None,
                "started_at": _pipe_now_iso(),
            })
        threading.Thread(
            target=_password_fix_worker,
            args=(items, bool(data.get("skip_existing", True))),
            name="password-fix-batch",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "queued": len(items)})

    @app.get("/api/accounts/set-password/status")
    def api_accounts_set_password_status():
        with _password_fix_lock:
            return jsonify({"ok": True, **_password_fix_state})


    if str(os.environ.get("TURB_WEBUI_BOOT", "")).strip() == "1":
        registration_scheduler.start()
        overnight_pipeline.ensure_started()
    return app
