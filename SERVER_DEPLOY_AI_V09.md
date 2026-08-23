# turb-gpt-free-register v0.9 部署交接

本文给服务器上的 AI 使用。目标是把注册机以 Ubuntu 24.04 原生 systemd 服务运行，保留 Resin 主代理链路、CloakBrowser 无头运行和本地运行数据。

## 1. 获取代码

新机器：

```bash
sudo git clone --branch v0.9 --depth 1 \
  https://github.com/1394805163/turb-gpt-free-register.git \
  /opt/turb-gpt-register
cd /opt/turb-gpt-register
```

已有目录更新：

```bash
cd /opt/turb-gpt-register
sudo systemctl stop turb-gpt-register.service || true
sudo cp -a .env ".env.backup-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
sudo tar -czf "/root/turb-gpt-register-runtime-$(date +%Y%m%d-%H%M%S).tgz" \
  .env data "注册成功的邮箱.json" "注册成功的邮箱.txt" "注册成功的token.txt" 2>/dev/null || true
sudo git fetch --tags origin
sudo git checkout v0.9
```

更新代码时不要删除或覆盖这些运行时内容：`.env`、`data/`、`注册成功的邮箱.json`、`注册成功的邮箱.txt`、`注册成功的token.txt`、`codex_accounts/`、`accounts/` 和 `logs/`。不要使用 `git reset --hard` 清理工作区。

## 2. 首次安装

脚本会创建非 root 服务用户、Python 虚拟环境、依赖、CloakBrowser binary 和 systemd unit。端口按当前部署约定使用 `5001`：

```bash
cd /opt/turb-gpt-register
sudo deploy/linux/bootstrap.sh \
  --service-user turbgpt \
  --host 127.0.0.1 \
  --port 5001
```

首次安装后编辑 `/opt/turb-gpt-register/.env`，然后锁定权限：

```bash
sudo chmod 600 /opt/turb-gpt-register/.env
```

生产配置至少确认以下值；密钥、邮箱专用密码和 Resin 鉴权信息只填入服务器 `.env`，不要写入 GitHub：

```dotenv
REGISTRATION_DRIVER=cloak
CLOAK_HEADLESS=true
CLOAK_GEOIP=true
EMAIL_SOURCE=icloud
ICLOUD_MAILBOXES_FILE=data/icloud_mailboxes.txt

REGISTRATION_PROXY_SOURCE=resin
REGISTRATION_PROXY_REQUIRED=true
RESIN_MANAGEMENT_URL=https://RESIN_HOST/
REGISTRATION_PROXY_EXCLUDED_COUNTRIES=HK

# 先保持与当前低并发策略一致
PLAN_CHECK_WORKERS=2
```

Resin 是主链路；不要把 `MIHOMO_*` 配置当作 Resin 配置填写。`REGISTRATION_PROXY_REQUIRED=true` 会阻止代理失败时意外直连。

## 3. 启动与验收

```bash
sudo systemctl daemon-reload
sudo systemctl enable turb-gpt-register.service
sudo systemctl restart turb-gpt-register.service
sudo systemctl is-active turb-gpt-register.service

cd /opt/turb-gpt-register
sudo deploy/linux/doctor.sh \
  --host 127.0.0.1 \
  --port 5001 \
  --service-user turbgpt

curl -fsS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:5001/login
sudo journalctl -u turb-gpt-register.service -n 100 --no-pager
```

验收要求：`doctor.sh` 全部为 `[OK]`，`/login` 返回 `200`，日志没有重复启动的 Gunicorn、Cloak doctor 失败或 Resin 预检失败。浏览器访问应通过现有反向代理，并保留 WebUI 授权码。

## 4. 导入本地账号 JSON

本地交付文件：

```text
exports/accounts-v09-with-mailbox-sync.json
```

它是普通、未压缩的 UTF-8 JSON，包含：

- `accounts`：当前账号池完整记录，兼容完整 OAuth 和只有 `access_token` 的账号；
- `icloud_mailboxes.entries`：邮箱池条目快照；
- `icloud_mailboxes.state`：`available/used/disabled/failed` 状态快照；
- `mailbox_sync`：导出时的数量校验信息。

通过受控 SCP/SFTP 传输，不要放入 GitHub：

```bash
sudo install -d -m 700 -o turbgpt -g turbgpt /opt/turb-gpt-register/imports
# 在本地执行，HOST、USER 使用实际值
scp exports/accounts-v09-with-mailbox-sync.json USER@HOST:/tmp/

# 在服务器执行
sudo install -m 600 -o turbgpt -g turbgpt \
  /tmp/accounts-v09-with-mailbox-sync.json \
  /opt/turb-gpt-register/imports/accounts-v09-with-mailbox-sync.json
rm -f /tmp/accounts-v09-with-mailbox-sync.json
```

然后在 WebUI 的账号页使用“导入账号凭据”选择该 JSON。导入逻辑如下：

1. `accounts` 内的完整 OAuth 凭据按邮箱或账号 ID更新，不重复新增；
   对已有账号，OAuth 的 `access_token` 会覆盖账号池旧的 `access_token`，并同步写入 `chatgpt_oauth_access_token`、`chatgpt_refresh_token`、`chatgpt_id_token` 等持久化字段；
2. 只有 `access_token` 的记录保留为 AT-only，不会因为缺少 `refresh_token` 被丢弃；
3. iCloud 账号会自动在邮箱池中标记为 `used`；已有手动 `disabled` 状态不会被自动恢复；
4. 导入不会自动把账号推送到 `chatgpt2api`，必须先查活成功且凭据完整，再由推送流程处理；
5. `icloud_mailboxes` 快照用于核对导出时邮箱池状态。目标服务器若还没有完整邮箱池，需同时将本地 `data/icloud_mailboxes.txt` 和 `data/icloud_mailboxes.json` 以同样的权限复制到目标 `data/`，再重启服务；不要用账号 JSON 覆盖服务器上人工停用的邮箱状态。

因此，这份 JSON 的导入目标是“覆盖/补齐现有账号池”，不是在账号池外另建一份 OAuth 文件；账号对应的 iCloud 邮箱会同步显示为已用。

导入后检查：

```bash
sudo systemctl restart turb-gpt-register.service
sudo journalctl -u turb-gpt-register.service -n 100 --no-pager
```

## 5. 数据与安全底线

- 账号 JSON、OAuth `access_token/refresh_token/id_token`、iCloud 邮箱池、IMAP 专用密码、`.env` 都是运行时秘密，禁止提交 GitHub、日志和工单。
- 备份只允许保存在服务器受限目录；目录 `700`，文件 `600`，服务使用非 root 用户运行。
- 更新失败时先停止服务并恢复本次更新前的 `.env`、`data/` 和账号池备份，再启动验收；不要删除旧备份。
- 生产环境先保持总在途工作峰值为 `2`，确认 Resin 预检、Cloak doctor、账号导入和查活稳定后再调整并发。
