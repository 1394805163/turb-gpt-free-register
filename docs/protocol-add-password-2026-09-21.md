# 协议补密码（add password）— 抓包记录与实现说明（2026-09-21）

> 来源：用浏览器版 `account_password.set_account_password()` 给 1 个新号补密码时，
> 经 CDP `Network.enable` 抓到完整链路。协议版实现见 `core/account_password.py::add_password_protocol()`。

## 一、真实请求链（按顺序）

| # | 方法 | URL | 关键头 | 说明 |
|---|---|---|---|---|
| 1 | GET | `https://chatgpt.com/backend-api/accounts/add_password/eligibility` | `authorization: Bearer <AT>`、`oai-device-id`、`referer: https://chatgpt.com/` | 前置资格检查，200 才可添加密码 |
| 2 | POST | `https://chatgpt.com/api/auth/signin/openai?login_hint=<email>&reauth=password&post_login_add_password=true&max_age=0&ext-oai-did=<device_id>` | `content-type: application/x-www-form-urlencoded`、`origin: https://chatgpt.com` | body: `callbackUrl=https://chatgpt.com/&csrfToken=<nextauth_csrf>&json=true`；返回重认证 authorize URL |
| 3 | GET | `https://auth.openai.com/api/accounts/authorize?...` | `referer: https://chatgpt.com/` | 重认证链；会话不新鲜时会要求邮箱 OTP |
| 4 | POST | `https://auth.openai.com/api/accounts/password/add` | `openai-sentinel-token`（**必需**）、`content-type: application/json`、**`referer: https://auth.openai.com/reset-password/new-password`** | body: `{"password":"<新密码>"}`，200 = 成功 |

另外设置页还会 GET：`/backend-api/accounts/mfa_info`、`/backend-api/accounts/security_settings/info`、
`/backend-api/accounts/change_password/eligibility`（只读，不影响补密码）。

## 二、协议版实现要点

- sentinel 使用 `username_password_create` flow（`request_sentinel_token` + `build_sentinel_header`）。
- 步骤 2/3 复用 `core/account_export` 的重认证助手（`_follow_reauth_with_retry` 等，含 403/CF 重试与 auth 文档预热）。
- 步骤 3 若落在 `email-verification` → 走邮箱池 OTP（`OtpWaitSession` + `_validate_reauth_otp`）。
- 写库：`db.update_account_registration_password()`（先行登记，防止服务端已生效但本地丢值）。

## 三、实测（2026-09-21）

| 账号 | 方式 | 结果 | 耗时 |
|---|---|---|---|
| `cheery_testy.6p@icloud.com` | 浏览器版（同时用于抓包） | updated | ~120 秒 |
| `for.punches.2q@icloud.com` | **协议版** | updated | ~20 秒 |
| `patties-warring-92@icloud.com` | **协议版** | updated | 18.1 秒 |
| `81sponges_fence@icloud.com` | **协议版** | updated | 25.1 秒 |

后续验证（同一批账号）：
- 协议开 2FA（UI 端点 `/api/accounts/<id>/totp-setup`）：`patties-warring-92@icloud.com` 成功（~50 秒）。
- **账号+密码+2FA 协议登录**（`core/password_login.login_with_password`）：8.3 秒拿到 AT + RT。
