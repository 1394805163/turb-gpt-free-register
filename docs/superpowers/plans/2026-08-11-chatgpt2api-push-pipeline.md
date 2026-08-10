# 注册机到 chatgpt2api 推送链路实现计划

> 面向 AI 代理的工作者：注册机是唯一注册入口；测活、推送和延时复查均持久化，任何临时网络错误都不得归类为账号死亡。

**目标：** 使用 iCloud 邮箱、CloakBrowser 指纹浏览器和美国代理完成注册；注册后测活成功自动推送 chatgpt2api；2–3 小时后复查 5 个样本并统计存活率。

**架构：** `注册成功 → 本地账号落盘 → 测活 → 推送队列 → chatgpt2api /api/accounts → 延时复查`。注册机保存推送状态；chatgpt2api 后续负责运行期账号淘汰。

**技术栈：** Flask WebUI、Python requests/httpx、JSON 持久化、Mihomo Controller、CloakBrowser。

---

### 任务 1：推送客户端和状态机

**文件：**
- 创建：`core/chatgpt2api_push.py`
- 修改：`core/db.py`、`core/live_check_service.py`、`main.py`
- 修改：`config/env_loader.py`、`.env.example`
- 测试：创建 `tests/test_chatgpt2api_push.py`

- [x] 配置目标 URL、管理员密钥、启用开关、请求超时和最大重试次数。
- [x] 请求 `POST /api/accounts`，Header 使用 `Authorization: Bearer <ADMIN_KEY>`。
- [x] 请求体发送 `accounts` 完整账号对象和 `refresh_after_import=false`。
- [x] 以账号 ID 和 access token 指纹实现幂等；只记录 token 哈希/长度，不记录明文。
- [x] 状态使用 `pending/live/push_failed/pushed/temporary_error/confirmed_dead`，推送失败持久化并指数退避。
- [x] 测活成功后自动入推送队列；注册成功但测活尚未完成时不推送。

### 任务 2：死亡分类和复制导出

**文件：**
- 修改：`core/account_liveness.py`、`core/db.py`
- 修改：`webui/app.py`、`webui/templates/index.html`
- 测试：创建 `tests/test_dead_account_export.py`

- [x] 仅 `account_deactivated/account_deleted/account_banned` 或间隔复核后再次明确无效归类 `confirmed_dead`。
- [x] 429、403、5xx、代理错误和超时归类 `temporary_error`。
- [x] 增加确认死亡筛选、一键复制和 TXT 下载；格式为每行一个邮箱。
- [x] 手动停用 iCloud 别名后同步将邮箱池记录标记为 `disabled`。

### 任务 3：仅美国代理和 5 账号端到端验收

**文件：**
- 修改：`config/proxy.py`、`core/cloakbrowser_driver.py`、`webui/config_editor.py`
- 测试：创建 `tests/test_mihomo_us_proxy.py`

- [x] 强制 Resin 关闭时回退 Mihomo Controller，不允许意外直连。
- [x] 控制器只从 `chatgpt us` 组选取美国节点；每个注册任务记录节点名、出口 IP 和耗时。
- [ ] 先注册 1 个并完成“测活→推送→chatgpt2api 入池”验收。
- [ ] 再注册 5 个，全部使用独立指纹上下文和美国节点。
- [ ] 注册完成 2–3 小时后再次测活，输出注册成功数、首测存活数、推送成功数、复测存活数和最终存活率。

### 任务 4：全流水线并发硬门禁

- [x] 注册、注册后套餐查询、测活和推送共用同一个全局 `BoundedSemaphore(2)`；重试等待期间不释放槽位。
- [x] 注册、测活、推送及关联后台 worker 默认值和硬上限均不超过 2。
- [x] 本机 `.env` 与 `.env.example` 的 `PLAN_CHECK_WORKERS` 统一为 2；项目内无 Compose 文件可覆盖该值。
- [x] 增加跨阶段实际入口回归测试，九个混合任务观测峰值严格等于 2。
