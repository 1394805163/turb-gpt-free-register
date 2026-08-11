# iCloud OTP UID 增量轮询实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]` / `- [x]`）语法来跟踪进度。

**目标：** 让 iCloud 隐藏邮箱在邮件已送达时可靠读取 OTP，并让新注册账号默认通过现有 Token 快速测活后推送到 chatgpt2api，完整 OTP 登录仅用于刷新 Token。

**架构：** iCloud 取信从服务端 HEADER 搜索改为 UID 增量发现，候选邮件下载后在本地严格匹配目标别名；连接通过 NOOP、周期性 SELECT 和定期重连三层恢复。套餐检查成功写入 Token 快速测活状态并触发幂等推送，注册完成不再自动执行邮箱 OTP 二次登录。

**技术栈：** Python 3.12、`imaplib`、`email`、`unittest`、Flask WebUI、现有 JSON 状态库与后台线程池。

---

## 文件结构

- 修改 `core/icloud_mail_pool.py`：实现 UID 枚举、候选邮件本地过滤、刷新/重连状态机和跨轮次去重。
- 修改 `core/icloud_mail_client.py`：把共享的已使用验证码状态传入邮箱池，并设置 iCloud 等待参数。
- 修改 `core/email_provider.py`：仅向 iCloud provider 透传去重状态。
- 修改 `core/account_liveness.py`：OTP 重试共享去重状态，最多重发一次。
- 修改 `config/email.py`、`.env.example`：默认 OTP 总等待 120 秒，增加 iCloud 刷新与重连配置。
- 修改 `core/plan_check_service.py`：Token 检查成功后写入快速测活状态并进入推送队列。
- 修改 `core/account_export.py`：移除注册成功后的自动 OTP 登录测活。
- 修改 `core/db.py`：记录测活方法，允许快速 Token 测活满足推送前置条件。
- 修改 `webui/templates/index.html`、`webui/templates/index_legacy.html`：明确区分“快速测活/查套餐”和“登录测活/刷新 Token”。
- 修改 `tests/test_icloud_mail_pool.py`：覆盖 UID、重连、串码、时间容差和去重。
- 创建 `tests/test_icloud_otp_retry.py`：覆盖 OTP 重发的跨轮次状态。
- 创建 `tests/test_fast_liveness_push.py`：覆盖快速测活、自动推送和不启动 OTP 登录。

### 任务 1：UID 增量取信与连接恢复

**文件：**
- 修改：`tests/test_icloud_mail_pool.py`
- 修改：`core/icloud_mail_pool.py`

- [x] **步骤 1：编写失败测试**

用可脚本化的 FakeIMAP 模拟：第一次 UID 列表没有新邮件，`NOOP`/重新 SELECT 后出现新 UID；另一用例要求旧连接持续陈旧、重连后的连接出现 OTP；两个别名共享收件箱时只返回目标别名验证码；代码不得发送 `HEADER` 搜索。

```python
def test_uid_polling_discovers_mail_after_refresh_without_header_search(self):
    pool = self.pool(reselect_interval=0, reconnect_interval=30)
    imap = ScriptedIMAP(uid_snapshots=[[b"10"], [b"10", b"11"]], messages={b"11": otp_mail("alias@icloud.com", "123456")})
    pool._connect_imap = Mock(return_value=imap)
    code = pool.wait_for_code(self.mailbox("alias@icloud.com"))
    self.assertEqual(code, "123456")
    self.assertFalse(any("HEADER" in call for call in imap.search_calls))
```

- [x] **步骤 2：运行测试并确认因仍使用 HEADER 搜索而失败**

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_icloud_mail_pool -v
```

预期：新 UID/重连测试 FAIL；现有池状态测试继续 PASS。

- [x] **步骤 3：实现最小 UID 状态机**

在 `ICloudMailboxPool` 中实现并使用以下边界：

```python
def _all_uids(self, imap) -> list[bytes]:
    status, data = imap.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []
    return sorted(data[0].split(), key=lambda value: int(value))

def _refresh_selected_mailbox(self, imap, *, force_select: bool) -> None:
    imap.noop()
    if force_select:
        status, _ = imap.select(self._mailbox_name(), readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error("iCloud IMAP 重新选择收件箱失败")

def _fetch_uid_message(self, imap, uid: bytes):
    status, fetched = imap.uid("fetch", uid, "(BODY.PEEK[])")
    raw = next((part[1] for part in fetched or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
    return self._parse_uid_message(uid, raw) if status == "OK" and raw else None

def _candidate_uid_window(self, all_uids: list[bytes], mailbox: dict[str, Any]) -> list[bytes]:
    checked = mailbox.setdefault("_seen_uids", set())
    limit = int(self.config.get("initial_scan_limit") or 20)
    return [uid for uid in all_uids[-limit:] if uid not in checked]

def _close_imap(self, imap) -> None:
    if imap is not None:
        try:
            imap.logout()
        except Exception:
            pass
```

要求：首次最多扫描最后 20 个 UID；后续只取比 `_last_uid` 新或尚未检查的 UID；本地严格匹配目标别名；每 2–3 秒 NOOP、10–15 秒 SELECT、15–20 秒无进展重连；UIDVALIDITY 变化时重建 UID 基线但保留 Message-ID/验证码去重状态。

- [x] **步骤 4：运行 UID 测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_icloud_mail_pool -v
```

预期：所有 iCloud 邮箱池测试 PASS，FakeIMAP 调用中没有 `HEADER` 搜索。

- [x] **步骤 5：提交任务 1**

```powershell
git add core/icloud_mail_pool.py tests/test_icloud_mail_pool.py
git commit -m "fix: 使用 UID 增量刷新 iCloud OTP"
```

### 任务 2：时间容差与 OTP 重发去重

**文件：**
- 创建：`tests/test_icloud_otp_retry.py`
- 修改：`core/icloud_mail_client.py`
- 修改：`core/email_provider.py`
- 修改：`core/account_liveness.py`
- 修改：`config/email.py`
- 修改：`.env.example`

- [x] **步骤 1：编写失败测试**

测试邮件 Date 比请求时间早 20 秒仍被接受；同一验证码哈希不能在第二轮复用；`_validate_with_retry` 最多发送一次重试 OTP，并把第一轮使用过的验证码传给第二轮。

```python
def test_login_retry_rejects_already_used_otp(self):
    with patch("core.account_liveness.wait_for_otp", side_effect=["111111", "222222"]) as wait, patch(
        "core.account_liveness.validate_email_otp",
        side_effect=[EmailOtpInvalidError("expired"), {"continue_url": "https://chatgpt.com/callback"}],
    ), patch("core.account_liveness.send_email_otp") as resend:
        result = _validate_with_retry(Mock(), "alias@icloud.com", 100.0)
    self.assertEqual(result["continue_url"], "https://chatgpt.com/callback")
    self.assertEqual(resend.call_count, 1)
    self.assertIn("111111", wait.call_args_list[1].kwargs["used_codes"])
```

- [x] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_icloud_otp_retry -v
```

预期：FAIL，`wait_for_otp` 尚不接受 `used_codes`，重试次数仍为 3。

- [x] **步骤 3：实现跨轮次状态**

给 `wait_for_otp` 增加可选 `used_codes`，仅在 iCloud 分支透传；`fetch_latest_otp` 把验证码哈希、已检查 UID、Message-ID 集合传给同一等待任务。iCloud 时间比较使用 `not_before - timedelta(seconds=30)`。`_validate_with_retry` 默认最多 2 次验证，第一次失败后重发一次，并在第二次读取时排除第一枚验证码。

- [x] **步骤 4：更新安全默认值**

`config/email.py` 将 `OTP_MAX_WAIT` 默认改为 120；`.env.example` 写明 120 秒、轮询 3 秒、SELECT 12 秒、重连 18 秒和 30 秒时钟容差。现有 `.env` 不写入仓库，运行验收前通过 WebUI/环境覆盖成 120。

- [x] **步骤 5：运行任务 2 测试**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_icloud_otp_retry tests.test_icloud_mail_pool -v
```

预期：全部 PASS。

- [x] **步骤 6：提交任务 2**

```powershell
git add core/icloud_mail_client.py core/email_provider.py core/account_liveness.py config/email.py .env.example tests/test_icloud_otp_retry.py
git commit -m "fix: 防止 iCloud OTP 重发复用旧验证码"
```

### 任务 3：快速 Token 测活后自动推送

**文件：**
- 创建：`tests/test_fast_liveness_push.py`
- 修改：`core/plan_check_service.py`
- 修改：`core/account_export.py`
- 修改：`core/db.py`
- 修改：`webui/templates/index.html`
- 修改：`webui/templates/index_legacy.html`

- [x] **步骤 1：编写失败测试**

测试 `accounts/check` 成功时写入 `live_check_method=token`、状态 `live` 并调用推送入队；注册保存只入套餐检查，不调用 `enqueue_account_live_check`；HTTP 401 只标记需要完整登录刷新，不确认死亡。

```python
def test_successful_plan_check_marks_token_live_and_enqueues_push(self):
    with patch.object(plan_check_service, "check_account_plan", return_value={"ok": True, "checked_at": "2026-08-11T10:00:00"}), patch(
        "core.chatgpt2api_push.enqueue_account_push", return_value={"accepted": True}
    ) as enqueue:
        result = plan_check_service._run_plan_check_inner(**self.kwargs)
    self.assertTrue(result["ok"])
    self.assertEqual(db.get_account(self.account_id)["live_check_method"], "token")
    enqueue.assert_called_once_with(self.account_id)
```

- [x] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_fast_liveness_push -v
```

预期：FAIL，套餐检查尚未写入快速测活或触发推送。

- [x] **步骤 3：实现快速测活边界**

套餐检查成功后调用下面的写回，保留当前 Access Token，并调用幂等推送队列；套餐 HTTP 401 记录 `needs_live_check=True`，但不改变为 `confirmed_dead`。`db.update_account_liveness` 持久化 `live_check_method`。

```python
db.update_account_liveness(account_id, {
    "ok": True,
    "status": "live",
    "method": "token",
    "checked_at": result.get("checked_at"),
    "access_token": access_token,
})
from core.chatgpt2api_push import enqueue_account_push
enqueue_account_push(
    account_id,
    expected_token_fingerprint=db.token_fingerprint(access_token),
)
```

- [x] **步骤 4：移除注册后的自动 OTP 登录**

`account_export` 注册成功后只排队套餐/Token 快速检查，不再调用 `enqueue_account_live_check`。完整登录测活仍可由独立前端按钮手动启动；authorize HTTP 403 继续沿用现有临时网络错误分类，不能进入邮箱超时或账号死亡状态。

- [x] **步骤 5：修正文案**

前端将现有套餐按钮显示为“快速测活/查套餐”，说明无需邮箱 OTP；现有查活按钮显示为“登录测活/刷新 Token”，明确需要邮箱 OTP。

- [x] **步骤 6：运行任务 3 测试**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_fast_liveness_push tests.test_chatgpt2api_push tests.test_pipeline_concurrency -v
```

预期：全部 PASS，总并发硬上限仍为 2。

- [x] **步骤 7：提交任务 3**

```powershell
git add core/plan_check_service.py core/account_export.py core/db.py webui/templates/index.html webui/templates/index_legacy.html tests/test_fast_liveness_push.py
git commit -m "feat: Token 快速测活后自动推送账号"
```

### 任务 4：完整回归和本地实测

**文件：**
- 修改：`docs/superpowers/plans/2026-08-11-icloud-otp-uid-polling.md`（勾选完成项）

- [x] **步骤 1：运行语法检查**

```powershell
.\.venv\Scripts\python.exe -m py_compile core/icloud_mail_pool.py core/icloud_mail_client.py core/email_provider.py core/account_liveness.py core/plan_check_service.py core/account_export.py core/db.py
```

- [x] **步骤 2：运行完整测试集**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

实际：完整测试集 145 项全部 PASS，没有失败或错误。

- [x] **步骤 3：在不重启服务的情况下验证快速测活**

选择一个已有账号调用快速测活 API；确认不产生新 OTP 邮件、Token 检查返回成功、推送保持幂等，且总并发快照峰值不超过 2。

- [x] **步骤 4：受控执行一次完整 OTP 登录实测**

只选择一个已确认仍启用的 iCloud 别名执行完整登录测活；确认日志出现 UID 增长、别名匹配和 `code_len=6`，不输出验证码明文。若 authorize 返回 403，则记录为网络临时错误，不把该结果作为 IMAP 验收失败。

- [x] **步骤 5：检查秘密和差异**

```powershell
git diff --check
git grep -n -E "[0-9]{6}" -- core tests | Select-String -NotMatch "177010|123456|111111|222222|654321"
git status --short
```

确认没有真实邮箱、OTP、Token、管理员密码或 Apple 专用密码进入提交。

- [x] **步骤 6：提交验证记录**

```powershell
git add docs/superpowers/plans/2026-08-11-icloud-otp-uid-polling.md
git commit -m "test: 验证 iCloud OTP 与快速推送链路"
```


## 2026-08-11 验证记录

- `py_compile`：目标模块全部通过。
- `unittest discover -s tests -v`：145 项全部通过。
- Token 快速测活实测：HTTP 200，写入 `live/method=token`，chatgpt2api 推送成功；IMAP UID 增量为 0，确认未产生 OTP 邮件。
- 完整 OTP 登录实测：65.9 秒完成，写入 `live/method=otp`，IMAP UID 增量为 1；日志只记录 Token 长度，不含 OTP 或 Token 明文。
- 总流水线并发硬上限回归：2。


### 任务 5：审查后加固与最终验收

- [x] iCloud 收件人改为仅解析 `To/Cc/Bcc/Delivered-To/X-Original-To/X-Apple-Original-To/Envelope-To` 等收件头，并以规范化邮箱地址精确匹配，禁止全文子串匹配。
- [x] 在同一 OTP 阶段持久共享 `last_uid`、`uidvalidity`、pending/seen UID、Message-ID 和验证码哈希；UIDVALIDITY 变化时原地重置 UID 状态。
- [x] Cloak、Roxy、BrowserUse 和完整登录测活统一使用 `OtpWaitSession`，所有重发共用 120 秒总预算与已用验证码集合。
- [x] 清理所有邮件/SMS 驱动中的验证码明文和可能包含验证码的 Subject 日志，只记录 `code_len`、邮件 ID、协议和剩余时间。
- [x] 套餐检查写回增加 Token 指纹 CAS；旧 Token worker 不得覆盖新 Token 的套餐、测活或推送状态。
- [x] chatgpt2api 推送 claim、重试记录和完成写回同时校验当前 Token 指纹与 claim 指纹。
- [x] HTTP 401 持久化为 `temporary_error` 且 `needs_live_check=True`，不确认账号死亡；HTTP 5xx 等临时错误不清除此标记。
- [x] `compileall -q core tests webui` 通过。
- [x] IMAP 构造、登录、选箱、NOOP、SEARCH、每次 FETCH、LOGOUT 与轮询休眠均受同一个 OTP deadline 约束；预算已耗尽时直接关闭 socket，剩余不足 10ms 时不再被 timeout 下限放大。
- [x] 套餐检查触发的 Token 指纹完整贯穿推送入队、worker 和 HTTP 发送边界；旧任务发现 Token 已刷新时返回 `stale_token`，不会发送请求。
- [x] 完整回归更新为 163 项全部 PASS。
- [x] Flask 路由冒烟验收通过：登录页 200、未授权 API 401、授权后的 `/api/summary` 200 且响应结构完整。
- [x] 新增行敏感信息扫描通过；`.env` 仍被 Git 忽略且没有进入差异或索引。
- [x] `git diff --check` 通过，总流水线并发硬上限仍为 2。
