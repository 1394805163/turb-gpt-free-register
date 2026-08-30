# iCloud OTP 可靠性修复实现计划

> **面向 AI 代理的工作者：** 本计划在当前本地工作树执行；不得操作 VPS、账号池、邮箱池或运行中的 WebUI 进程。

**目标：** 修复 iCloud OTP 偶发取码超时、UID 漏扫以及验证码已登录却被误判为 `invalid` 的问题。

**架构：** 保持现有 `email_provider -> icloud_mail_client -> ICloudMailboxPool` 接口不变。Cloak 查活继续复用 `OtpWaitSession`，在取码超时且页面仍处于 OTP 阶段时重发一次；页面已经离开 OTP 并能取得登录态时优先判定成功。iCloud IMAP 继续使用 UID 轮询，但扩大首次扫描窗口、仅在邮件完成处理后标记 UID，并保留 pending UID 重试。

**技术栈：** Python 3、标准库 `unittest`、`imaplib`、现有 CloakBrowser/Flask 代码。

---

### 任务 1：Cloak OTP 状态与超时回归

**文件：**
- 创建：`tests/test_cloakbrowser_liveness.py`
- 修改：`core/cloakbrowser_liveness.py:103-130`

- [x] 编写两个失败测试：取码超时后仍能触发重发并继续；OTP 提交返回 `invalid` 但页面已进入登录态时按成功处理。
- [x] 运行定向测试并确认失败原因是现有流程直接抛出超时或继续点击重发。
- [x] 在 `run_cloak_liveness_flow` 内捕获取码超时，复用现有总预算、去重状态和重发限制。
- [x] 在重发前检查页面登录态，避免已登录页面被当成 OTP 错误。
- [x] 运行定向测试确认通过。

### 任务 2：iCloud UID 窗口与邮件处理顺序

**文件：**
- 创建：`tests/test_icloud_mail_pool_regressions.py`
- 修改：`core/icloud_mail_pool.py:303-463`

- [x] 编写失败测试：目标邮件位于初始窗口之外时不会因 `_last_uid` 提前推进而永久跳过；首次 FETCH 得到可解析但暂时没有验证码的邮件时仍可再次处理。
- [x] 运行定向测试确认现有实现失败。
- [x] 调整初始候选窗口和 UID 基线推进顺序，保留 `pending_uids` 的临时 FETCH 重试语义。
- [x] 运行 iCloud 邮件池及 OTP 重试回归测试。

### 任务 3：IMAP 诊断与交付验证

**文件：**
- 修改：`core/icloud_mail_pool.py:194-221,380-474`
- 修改：`docs/superpowers/plans/2026-08-30-icloud-otp-reliability.md`

- [x] 增加脱敏的连接、UID 候选数量和跳过原因日志，不记录邮箱明文、验证码或凭据。
- [x] 运行 Python 编译检查、定向 unittest、相关账号查活逻辑测试。
- [x] 执行 `codegraph sync` 和 `codegraph status`，审查 diff、冲突标记和工作区范围。
- [x] 仅提交本次代码、测试和计划文件；保留既有 `SERVER_DEPLOY_AI_V09.md` 与 `.playwright-cli/` 未提交状态。
- [x] 推送到用户 fork 的新分支并核对远端提交，不重启本地或远端服务。

### 本轮验证记录

- `python -m compileall -q core tests`：通过。
- 相关定向测试：`28` 项通过。
- `unittest discover` 未作为交付依据：项目测试导入会恢复历史推送任务并触发本地账号网络重试，已在恢复到下一组前终止测试进程；运行中的 WebUI 未终止。
