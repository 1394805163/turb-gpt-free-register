# -*- coding: utf-8 -*-
"""ChatGPT OAuth refresh_token 协议刷新（不开浏览器）。

实测 2026-09-15：POST https://auth.openai.com/oauth/token（grant_type=refresh_token +
client_id，curl_cffi impersonate=chrome146）→ 200/869ms → 新 AT + RT 轮换 + id_token；
成功后经 CAS 写回注册机 DB。参考实现：chatgpt2api-proxy-pool-dev/services/account_service.py。
"""
from __future__ import annotations

import hashlib
import logging
import time

logger = logging.getLogger(__name__)

TOKEN_URL = "https://auth.openai.com/oauth/token"


def _fp(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:10]


def refresh_account_credentials(
    email: str,
    *,
    write_back: bool = True,
    timeout: int = 60,
) -> dict:
    """用账号的 refresh_token 换新凭据；write_back 时经 CAS 写回 DB。

    返回 {ok, status?, ms?, access_token?, refresh_token?, id_token?, rt_rotated?, error?}
    """
    target = str(email or "").strip()
    if not target:
        return {"ok": False, "error": "email 为空"}

    from core import db

    acc = db.get_account_by_email(target) or {}
    rt = str(acc.get("chatgpt_refresh_token") or "").strip()
    cid = str(acc.get("chatgpt_oauth_client_id") or "").strip()
    at_old = str(acc.get("chatgpt_oauth_access_token") or acc.get("access_token") or "").strip()
    if not rt or not cid:
        return {"ok": False, "error": "账号缺少 refresh_token 或 client_id"}

    from curl_cffi import requests as curl_requests

    # impersonate 档自带配套 UA/头；显式传旧版 UA 反而制造不一致。
    session = curl_requests.Session(impersonate="chrome150")
    t0 = time.time()
    try:
        resp = session.post(
            TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid},
            timeout=timeout,
        )
        ms = int((time.time() - t0) * 1000)
        data = resp.json() if resp.text else {}
        if resp.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("error_description") or data.get("error") or data.get("message") or "")
            return {"ok": False, "status": resp.status_code, "ms": ms,
                    "error": detail or (resp.text or "")[:200]}
        new_at = str(data.get("access_token") or "")
        new_rt = str(data.get("refresh_token") or rt)
        new_id = str(data.get("id_token") or "")
        result = {
            "ok": True, "status": resp.status_code, "ms": ms,
            "access_token": new_at, "refresh_token": new_rt, "id_token": new_id,
            "rt_rotated": new_rt != rt, "new_rt_fp": _fp(new_rt),
        }
        if write_back:
            try:
                wb = db.update_account_chatgpt_oauth(
                    target,
                    {
                        "access_token": new_at,
                        "refresh_token": new_rt,
                        "id_token": new_id or str(acc.get("chatgpt_id_token") or ""),
                        "client_id": cid,
                    },
                    expected_access_token=at_old,
                )
                result["write_back"] = wb
            except Exception as exc:
                result["write_back"] = {"updated": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return result
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:220]}",
                "ms": int((time.time() - t0) * 1000)}
    finally:
        session.close()
