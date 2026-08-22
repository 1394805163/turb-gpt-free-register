# 注册机凭据持久化与查活代理轮换需求

状态：需求规格，供本地 PC 实现；VPS 只负责拉取、构建、部署和验收。

## 目标

解决两个独立问题：

1. 注册成功后只保存短期 `access_token`，过期后必须依赖邮箱 OTP，维保成本高。
2. 查活预检失败时重复使用同一 Resin 身份，导致连续 403/超时，占满查活槽位。

本需求不改变账号判定原则：网络风控、代理错误、403/429、5xx 和超时均为临时错误；只有明确的账号停用、删除或封禁响应才判定为 `confirmed_dead`。

## 非目标与约束

- 不在 VPS 直接修改核心代码；实现、测试和提交在本地 PC 完成。
- 不从现有 `access_token` 推导或伪造 `refresh_token`。
- 只有 OAuth 实际返回的 refresh token 才能保存。
- 不在日志、API 响应、截图或 Git 提交中输出 token、Cookie、完整邮箱、代理鉴权 URL。
- 保持 Linux、Windows、macOS 路径和启动方式兼容；不得依赖 VPS 私有路径。
- 保留旧版 AT-only 数据和 CPA/整行导入格式的读取兼容性。

## 代码落点

| 能力 | 主要文件 |
| --- | --- |
| 注册后读取 OAuth/session 凭据 | `core/account_export.py`、`core/cloakbrowser_registration.py`、`core/browser_use_registration.py` |
| 账号模型、原子写回、CAS | `core/db.py` |
| AT 过期识别与套餐查询 | `core/chatgpt_plan.py`、`core/plan_check_service.py` |
| OTP 登录回退 | `core/account_liveness.py`、`core/openai_auth.py` |
| 查活队列和资源释放 | `core/live_check_service.py`、`core/pipeline_concurrency.py` |
| 代理池、身份租约和轮换 | `config/proxy.py` |
| WebUI 导入、复制、状态显示 | `webui/app.py`、`webui/templates/index.html` |

## 1. 凭据数据模型

账号记录新增或规范化以下字段：

```text
access_token       当前请求使用的短期令牌，可为空
refresh_token      OAuth 实际签发的长期令牌，可为空
client_id          与 refresh token 绑定的 OAuth client，可为空
token_expires_at   AT 的过期时间，优先使用 JWT exp 或上游 expires_in
credential_source  session / oauth_token / cpa_import / legacy_at 等来源
credential_updated_at
```

要求：

- JSON、TXT、CPA 导入导出同时支持 AT/RT；旧字段顺序继续可读。
- `copy_line` 不默认包含 refresh token，只有明确的管理员导出操作才允许导出，并且要二次确认。
- 写入使用临时文件 + 原子替换；更新时使用旧 token 指纹 CAS，避免并发查活覆盖新凭据。
- 所有 token 字段在列表接口只返回存在性、指纹和过期时间，不返回明文。

## 2. 注册成功凭据捕获

注册完成后按以下顺序捕获凭据：

1. 记录 `/api/auth/session` 返回的 `accessToken`、`expires`、用户和账号元数据。
2. 仅在 OAuth token endpoint 或明确的 OAuth 回调响应中发现 `refresh_token` 时保存。
3. 同时保存 `client_id` 和 `credential_source`；没有实际 RT 时保持空值。
4. 批次归档文件也采用新字段，但禁止把 token 写入普通运行日志。

如果当前 ChatGPT Web session 只返回 `accessToken`，则该账号仍按 AT-only 账号处理，不能声称已获得 RT。

## 3. AT 过期维保流程

```text
套餐/请求返回 401
  -> 标记 needs_live_check=true，不判死
  -> 有 refresh_token：调用已验证的 OAuth refresh 流程
       -> 成功：原子写回新 AT/RT/过期时间
       -> 失败：进入 OTP 浏览器回退
  -> 无 refresh_token：直接进入 OTP 浏览器回退
  -> OTP 成功：写回新 AT；若 OAuth 实际返回 RT，同时保存 RT
  -> 更新套餐查询和 chatgpt2api 推送的 token 指纹
```

刷新请求必须有连接、读取和总超时；最多有限重试。刷新失败不得循环占用查活槽位。

## 4. 查活代理轮换

### 会话边界

- 一次完整 OTP/OAuth 会话固定一个代理、Cookie jar、设备 ID 和指纹。
- OTP 输入错误、邮箱超时、资料页错误属于账号/流程结果，结束当前会话，不在会话内部切换代理。

### 需要换代理的情况

在 Providers、CSRF、Signin、Authorize 预检阶段遇到以下错误时：

- HTTP 403、429、5xx；
- 连接失败、TLS 错误、读取/导航超时；
- 明确的 Cloudflare challenge 或代理连接失败页面。

处理步骤：

1. 关闭当前浏览器会话并释放资源。
2. 从当前 Resin 导出池重新申请下一条身份。
3. 同一账号本轮不得重复使用已失败身份。
4. 记录代理指纹、出口国家、状态码和耗时，不记录完整 URL。
5. 达到账号级尝试上限后写入 `temporary_error`，等待人工或定时任务重试。

### Providers 检查策略

Providers 是低成本预检，不应成为不可配置的唯一登录协议：

- `200` 且 JSON 含 `openai`：继续正常流程；
- `401`：记录后允许继续 CSRF/Signin；
- `403`：标记该身份风险，允许一次受控的 CSRF/Signin 验证；后续仍失败才换代理；
- 429、5xx、连接错误或超时：立即换代理。

是否启用 Providers 预检由配置项控制，默认开启但不能绕过代理轮换。

## 5. 队列、超时和资源释放

- 每个任务必须在 `finally` 中释放浏览器、会话、代理租约、队列槽位和线程状态。
- 为预检、OTP 等待、OAuth callback、session 获取分别设置硬超时。
- `queued` 和 `running` 状态必须有时间戳；超过 stale 阈值可回收并标记临时错误。
- 查活任务不能因为下游推送失败而保持 running。
- WebUI 重启恢复任务时只能恢复为可重试临时错误，不判定账号死亡。

## 6. 测试要求

本地 PC 必须先完成测试，再提交镜像：

1. 旧 AT-only JSON、TXT、CPA 导入回归测试。
2. OAuth 返回 RT 时字段捕获和脱敏测试。
3. OAuth 不返回 RT 时保持 AT-only 的兼容测试。
4. 401 -> refresh 成功的原子写回测试。
5. refresh 失败 -> OTP 回退测试。
6. Providers 403/401/200、429、超时的分支测试。
7. 同一账号失败代理不重复、不同账号租约不冲突测试。
8. 并发写回 CAS 测试，确保旧结果不能覆盖新 token。
9. Windows、Linux、macOS 的路径和启动冒烟测试。
10. 单账号灰测：只验证是否实际出现 `refresh_token`，不批量迁移。

## 7. 部署与验收

1. 本地提交代码、测试和变更说明到仓库分支。
2. VPS 备份 `注册成功的邮箱.json`、批次归档、代理导出文件和当前镜像标签。
3. VPS 拉取指定 commit，构建新镜像；不使用 `docker compose down -v`。
4. 先启动 WebUI，不启动注册浏览器和批量查活。
5. 验证旧账号列表可读、token 指纹不变、队列为空、内存和 Swap 正常。
6. 使用一个测试账号和一个已知可用代理做灰测。
7. 确认日志只显示脱敏状态、代理指纹和阶段耗时后，再开放批量维保。
8. 任一阶段出现大面积 403/429、900 秒超时、队列不释放或服务重启，立即停止灰测并回滚镜像和数据快照。

## 验收标准

- 现有 AT-only 账号不丢失、不被误判死亡。
- 新注册账号能保存实际返回的 RT；没有 RT 时明确显示 AT-only。
- 401 后优先刷新 RT，失败才启动 OTP 浏览器。
- 预检风控错误会在浏览器会话之间换代理，不会在同一出口重复重试。
- 单任务超时后浏览器、队列槽位和代理租约均释放。
- 旧平台、Windows/Linux/macOS 启动方式和现有 chatgpt2api 推送兼容。
