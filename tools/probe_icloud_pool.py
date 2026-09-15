# -*- coding: utf-8 -*-
"""邮箱探测器：submit_email 看 page.type，筛出"真新"的 iCloud 隐藏邮箱"""
import os, sys, time, json, logging
WT = r"D:\Program files\moe-atelier-main\register_test-worktrees\register-v08-credential-refresh"
os.chdir(WT); sys.path.insert(0, WT)
logging.basicConfig(level=logging.ERROR)

from config import proxy as proxy_cfg
from core.icloud_mail_client import list_mailboxes
from core.cloakbrowser_driver import build_cloak_driver
from core.page_session import PageSession
from core.chatgpt_auth import signin_openai
from core.codex_oauth import _post_json

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 30

rows = [r for r in list_mailboxes(limit=900) if str(r.get("status")) == "available"]
sample = [str(r["email"]) for r in rows[:LIMIT]]
print(f"待扫: {len(sample)} 个 available 邮箱（前 {LIMIT} 个）", flush=True)

driver = None
for attempt in range(1, 4):
    try:
        sel = proxy_cfg.pick_registration_proxy()
        driver, opened = build_cloak_driver(proxy="", proxy_selection=sel)
        print("浏览器就绪（选路:", ascii(sel.get("node_name")), "）", flush=True)
        break
    except Exception as exc:
        print(f"driver 尝试 {attempt} 失败: {str(exc)[:100]}", flush=True)
        time.sleep(3)
if driver is None:
    raise SystemExit("浏览器启动失败")

results = []
try:
    session = PageSession(driver)
    for i, EMAIL in enumerate(sample, 1):
        item = {"email": EMAIL}
        try:
            driver.get("https://chatgpt.com/auth/login"); time.sleep(3)
            csrf = (session.get("https://chatgpt.com/api/auth/csrf", headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")).json() or {}).get("csrfToken") or ""
            auth_url = signin_openai(session, csrf, EMAIL, prompt="login_or_signup")
            driver.get(auth_url); time.sleep(4)
            r1 = _post_json(session, "https://auth.openai.com/api/accounts/authorize/continue",
                            {"username": {"kind": "email", "value": EMAIL}}, referer="https://auth.openai.com/log-in")
            d1 = r1.json() if r1.status_code == 200 else {}
            item["status"] = r1.status_code
            item["page_type"] = (d1.get("page") or {}).get("type")
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        results.append(item)
        verdict = item.get("page_type") or item.get("error")
        print(f"[{i}/{len(sample)}] {EMAIL[:38]:<40} -> {verdict}", flush=True)
finally:
    try:
        driver.quit()
    except Exception:
        pass

os.makedirs("run", exist_ok=True)
with open(os.path.join("run", "pool_scan.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

from collections import Counter
c = Counter(str(r.get("page_type") or r.get("error", "?"))[:30] for r in results)
print("=== 汇总 ===", flush=True)
for k, v in c.most_common():
    print(f"  {k}: {v}", flush=True)
print("结果已存 run/pool_scan.json", flush=True)
