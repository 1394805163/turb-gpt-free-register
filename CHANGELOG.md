# Changelog

## 0.10.0 - 2026-09-21

发布分支：`codex/merge-upstream-0921`（tag `v0.10.0`）

### 上游全量合并（myfanhua/turb-gpt-free-register @ bdc1891，19 个提交）

- **模拟指纹升级**：Sentinel VM 补齐 Screen / Date / PluginArray / MimeTypeArray / WebGL(0x9245-0x9246) / navigator 原型语义 / webdriver getter；HTTP 画像与 VM 改用同一组 screen / outer / viewport / GPU 数据；`SENTINEL_SV → 20260810913b`、`OPENAI_BUILD_ID`、`OAI_CLIENT_BUILD_NUMBER`、Statsig/AB 版本号同步刷新；document 导航 ID 仅在真实换页时轮换，并从真实页面动态同步 `data-build`。
- **纯协议补强**：`register_user`（user/register + sentinel/SO 双头）、`navigate_email_otp_send`、`request_password_sentinel_bundle`（同一 p/SID 三 flow 探测）、`generate_registration_password`、`_request_with_proxy_retry`（3 次退避且可中断）；`SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE` 开启；Codex OAuth 新增密码+2FA 全协议登录与「明确进入 OTP 页才轮询邮箱」步骤机；查活新增完整 Web 登录兜底 `_login_via_full_web_flow`。
- **链式代理**：新增 `core/proxy_chain.py` / `core/proxy_utils.py`（本地 SOCKS5 中继：本地上游 → 目标代理 → ChatGPT），接入 session / plan / live-check / cloak / roxy / twofa；未配置上游时行为不变。新增代理 URL 归一化 `normalize_proxy_url/list`。
- **账号页完整导出**：邮箱 / 接码 API / 密码 / 2fa.run / 2FA 密钥（下载 TXT + 复制剪贴板）；generic_api 取码链接规整。
- **其它**：SMSBower / GrizzlySMS 接码，换绑逻辑优化，Roxy 密码页与 OTP 输入刷新重试及多语言识别。

### 保留与兼容（本项目独有，未被覆盖）

- 保留：Resin / Mihomo 出口选路与批次国家隔离、透明路由轮换、iCloud 邮箱池与 IMAP 取码、protocol_page 页面会话、chatgpt2api 推送与下游只读同步、补密码 / 密码登录、并发闸、日志脱敏、查活临时错误分类与托管下游短路。
- 上游新能力默认不启用（链式代理 / SMSBower / Roxy 等配置留空即关），`REGISTRATION_DRIVER` 维持 `cloak`。

### 灰测修复（子智能体灰测发现，均已修）

- 补回被误删的 `roxy_registration._click_continue_with_password_if_present`（否则「使用密码继续」分支 NameError）。
- `check_account_plan` 接线 `_warm_plan_session`。
- `GENERIC_API_PROXY` 默认值回空，避免 generic_api 取码固定走本机代理、绕过出口轮换。
- 7 处上游新日志改走邮箱脱敏；`user/register` 失败不再落响应体原文（改记长度 + 指纹）。
- `.gitattributes` 增加 `sentinel/sdk.js -text`（防止 Windows autocrlf 破坏字节级哈希断言）。
- `chatgpt_auth` 补 `navigator_language` 守卫；`config.email` 补 `OMNIMAIL_BASE`。
- 顺带修复：`core/skyvern_registration.py` 重复关键字参数（Py3.12 SyntaxError）、`requirements.txt` 补 `PySocks`。

### 验收

- 536 个单测（+34，含上游新增 5 个测试文件）失败集合与合并前基线 27 条**完全一致**（零新增、零消失）。
- 227 个 .py 全量编译 + 核心模块 import + sentinel JS `node --check` 通过；备用端口 5055 冷启动冒烟 9 个接口全 200、零异常。
- 合并计划与灰测报告：`docs/merge-plans/2026-09-21-upstream-bdc1891.md`。

## 0.9.3 - 2026-09-04

### 上游兼容基线

- 完成原项目近期更新的范围审查：仅将 `Plus Trial` 筛选和批量 TOTP 作为低风险候选，不整体合并上游当前主线。
- 保留当前项目的 SQLite 运行存储、Resin/Mihomo 代理轮换、iCloud 邮箱同步、OAuth/AT-only 导入导出、按邮箱覆盖和 `chatgpt2api` 推送链路。
- Codex OAuth 与刷新相关逻辑继续以当前项目已有实现为基线，后续只做经过定向测试的局部兼容，避免重复造轮子或覆盖特色功能。

## 0.9.2 - 2026-08-31

### 兼容优化

- **TOTP 状态筛选**：现代和旧版账号页统一支持已启用、未启用、处理中和失败筛选；轻量状态接口使用同一筛选结果，且不下发 TOTP Secret。
- **延迟领取邮箱**：CloakBrowser、RoxyBrowser、Browser Use/Skyvern 仅在确认可见邮箱输入框后领取自动邮箱，页面被拦截时不再提前消耗邮箱池素材。
- **任务状态回传**：注册子进程将实际领取的邮箱回传父进程并写入任务记录，保留原有超时、停止和邮箱回收逻辑。
- **上游性能兼容**：保留静态账号查看页防抖；对明确支持 gzip 的大 JSON 响应启用压缩；现代和旧版 UI 降低日志及批量状态轮询频率，减少 WebUI 对本地服务的请求压力。
- **CPA 推送兼容**：推送请求补齐远端要求的 `tokens`，账号对象采用 CPA/Codex 凭据字段并保留 `email`；支持 AT-only 与完整 OAuth 两种凭据，旧 `access_token` 不在注册机侧删除。

## 0.9.1 - 2026-08-24

发布提交：`5c42393`（`codex/v08-resin-primary-deploy`）

### 修复

- **OAuth 覆盖账号池**：导入账号池 JSON 时优先使用 `chatgpt_*` 持久化字段，完整 OAuth 会覆盖旧 `access_token`，并保留 `refresh_token/id_token`。
- **未压缩迁移包**：补充服务器部署交接文档和本地账号/邮箱池同步导出流程；运行时凭据不进入 Git。

## 0.9 - 2026-08-24

发布提交：`acd53bc`（`codex/v08-resin-primary-deploy`）

### 重点改动

- **Resin 主代理链路**：注册、快速查活和 OAuth 统一使用出口预检；Resin 代理异常时轮换出口，失败时保持阻断直连。
- **Mihomo 兼容路由**：保留 Mihomo 单节点选择、地区筛选和透明路由轮换能力，作为通用代理策略的兼容实现。
- **OAuth 持久化**：补齐 OAuth 凭据保存、导入导出、年龄策略、拦截识别和代理轮换重试。
- **查活与推送**：Token 快速查活、套餐/生图额度记录、成功凭据去重推送和账号状态同步。
- **WebUI 与调度**：增加跨页选择、OAuth 状态筛选、邮箱池同步、任务调度和浏览器任务并发闸门。

### 部署验收

- 本地回归：`311 passed, 3 skipped`。
- 服务器定向回归：`35 passed`。
- Ubuntu systemd、CloakBrowser doctor、Resin feed 和 WebUI `/login` 已验收。
- 运行时账号、Token、邮箱池、日志、`.env` 和本地备份不属于版本资产。

## 0.8 - 2026-08-23

基线提交：`8ce3bc2`（`codex/registration-country-agnostic-20260818`）

### 重点改动

- **CloakBrowser 阶段超时**：为登录页、邮箱下一步、资料页和 session 获取增加独立的墙钟截止时间；阶段超时会主动异步关闭浏览器，避免 Selenium 调用长期占用任务。
- **注册任务停滞看门狗**：监控注册子进程日志进展；连续无日志达到停滞阈值时终止子进程，并将结果标记为自动超时而不是代理失效。
- **停止原因与终态一致性**：统一识别自动超时、外部看门狗和阶段停滞原因；手动停止、自动终止和排队取消使用明确原因写入任务状态与日志。

### 基线范围

- 本版本标记的是 VPS 运行源码在 `8ce3bc2` 的可追溯基线。
- 运行时账号、Token、邮箱池、日志、`.env` 和本地备份不属于版本资产，未纳入提交。
- 现有未跟踪的 `*.backup-*` 文件继续保留在 VPS，仅用于回滚。
