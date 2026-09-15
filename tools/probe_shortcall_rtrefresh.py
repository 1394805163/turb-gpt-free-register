# -*- coding: utf-8 -*-
"""短调用实验 E3：协议 RT 刷新 vs 浏览器查活（默认 dry-run，--write-back 才回写）。

用账号的 chatgpt_refresh_token + chatgpt_oauth_client_id 直接 POST
https://auth.openai.com/oauth/token（grant_type=refresh_token）。
成功 = "刷新 AT 不必开浏览器"成立 → 查活可大幅省浏览器会话；失败 = 维持浏览器查活。

参考实现：chatgpt2api-proxy-pool-dev/services/account_service.py::_request_access_token_refresh
注意：刷新可能轮换 refresh_token；--write-back 会把新三件套经 CAS 门禁写回注册机 DB。

用法：
    python tools/probe_shortcall_rtrefresh.py --email <目标>
    python tools/probe_shortcall_rtrefresh.py --email <目标> --write-back
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("probe_rtrefresh")

TOKEN_URL = "https://auth.openai.com/oauth/token"


def _fp(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:10]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--write-back", action="store_true", help="成功时把新凭据 CAS 写回 DB（默认 dry-run）")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    from core import db

    acc = db.get_account_by_email(args.email) or {}
    rt = str(acc.get("chatgpt_refresh_token") or "").strip()
    cid = str(acc.get("chatgpt_oauth_client_id") or "").strip()
    at_old = str(acc.get("chatgpt_oauth_access_token") or acc.get("access_token") or "").strip()
    result: dict = {"email": args.email, "rt_len": len(rt), "cid": cid, "write_back": args.write_back}
    if not rt or not cid:
        result["error"] = "账号缺少 refresh_token 或 client_id"
        print("RESULT:", json.dumps(result, ensure_ascii=False))
        return 1

    from curl_cffi import requests as curl_requests

    try:
        from config import USER_AGENT as _ua
        ua = str(_ua or "Mozilla/5.0")
    except Exception:
        ua = "Mozilla/5.0"

    session = curl_requests.Session(impersonate="chrome146")
    t0 = time.time()
    try:
        resp = session.post(
            TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": ua,
            },
            data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid},
            timeout=args.timeout,
        )
        result["ms"] = int((time.time() - t0) * 1000)
        result["status"] = resp.status_code
        data = resp.json() if resp.text else {}
        if resp.status_code == 200 and isinstance(data, dict) and data.get("access_token"):
            new_at = str(data.get("access_token") or "")
            new_rt = str(data.get("refresh_token") or rt)
            new_id = str(data.get("id_token") or "")
            result.update({
                "ok": True,
                "new_at_len": len(new_at),
                "rt_rotated": new_rt != rt,
                "new_rt_fp": _fp(new_rt),
                "id_token": bool(new_id),
            })
            if args.write_back:
                wb = db.update_account_chatgpt_oauth(
                    args.email,
                    {
                        "access_token": new_at,
                        "refresh_token": new_rt,
                        "id_token": new_id or str(acc.get("chatgpt_id_token") or ""),
                        "client_id": cid,
                    },
                    expected_access_token=at_old,
                )
                result["write_back_result"] = wb
        else:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("error_description") or data.get("error") or data.get("message") or "")
            result.update({"ok": False, "error": detail or (resp.text or "")[:220]})
    except Exception as exc:
        result.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
    finally:
        session.close()

    print("RESULT:", json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
