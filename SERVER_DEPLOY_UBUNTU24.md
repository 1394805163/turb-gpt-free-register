# Ubuntu 24.04 LTS 服务器部署操作手册

本文用于在 **2 核 2 GB** 的 Ubuntu 24.04 LTS 服务器上部署本项目。生产方案为原生 Python 虚拟环境、Gunicorn 和 systemd，**不使用 Docker**。

## 1. 部署结果

部署完成后的运行结构：

```text
systemd
└─ Gunicorn（1 worker、4 threads）
   └─ WebUI / 注册任务（全流水线总并发固定为 2）
      └─ CloakBrowser / Chromium（按任务启动并回收）
```

固定参数：

- 系统：Ubuntu 24.04 LTS，x86_64；
- 应用目录：`/opt/turb-gpt-register`；
- 服务用户：`turbgpt`，禁止使用 root 运行服务；
- 状态目录：`/var/lib/turb-gpt-register`；
- 默认监听：`127.0.0.1:5000`；
- Gunicorn：1 个 worker、4 个 threads；
- 注册、查活和推送共享总并发：2；
- systemd 内存软限：`MemoryHigh=1700M`；
- 建议 swap：2 GB。

## 2. 部署前检查

```bash
uname -m
lsb_release -ds || cat /etc/os-release
nproc
free -h
df -h /
```

期望结果：

- 架构为 `x86_64`；
- 系统为 Ubuntu 24.04 LTS；
- 至少 2 个 CPU 线程、约 2 GB 内存；
- 建议至少保留 10 GB 磁盘空间。

更新基础软件：

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates openssh-client
```

## 3. 配置 2 GB swap

先检查现有 swap：

```bash
swapon --show
```

已有 1～2 GB swap 时跳过本节。没有 swap 时执行：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || printf '/swapfile none swap sw 0 0\n' | sudo tee -a /etc/fstab
free -h
```

## 4. 拉取私有仓库

推荐为服务器配置 GitHub **只读 Deploy Key**。不要把 GitHub Token 写入脚本、仓库或 shell 历史。

### 4.1 创建服务用户和只读密钥

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/turb-gpt-register \
  --shell /usr/sbin/nologin turbgpt 2>/dev/null || true

sudo install -d -o turbgpt -g turbgpt -m 700 \
  /var/lib/turb-gpt-register/.ssh

sudo -u turbgpt -H ssh-keygen -t ed25519 -N '' \
  -f /var/lib/turb-gpt-register/.ssh/id_ed25519 \
  -C 'turb-gpt-register-deploy'

sudo cat /var/lib/turb-gpt-register/.ssh/id_ed25519.pub
```

将输出的公钥添加到 GitHub 仓库：

```text
Settings → Deploy keys → Add deploy key
```

只勾选读取权限，**不要勾选 Allow write access**。

### 4.2 核对 GitHub 主机指纹并克隆

```bash
EXPECTED_GITHUB_ED25519='SHA256:+DiY3wvvV6TuJJhbpZisF/zLCPUXBrMkLOvenrUiz14'
KNOWN_HOSTS_TMP="$(mktemp)"
ssh-keyscan -t ed25519 github.com >"$KNOWN_HOSTS_TMP"
ACTUAL_GITHUB_ED25519="$(ssh-keygen -lf "$KNOWN_HOSTS_TMP" -E sha256 | awk '{print $2}' | sort -u)"
test "$ACTUAL_GITHUB_ED25519" = "$EXPECTED_GITHUB_ED25519" || {
  echo "GitHub SSH 指纹不匹配，停止部署" >&2
  rm -f "$KNOWN_HOSTS_TMP"
  exit 1
}
sudo install -o turbgpt -g turbgpt -m 600 \
  "$KNOWN_HOSTS_TMP" /var/lib/turb-gpt-register/.ssh/known_hosts
rm -f "$KNOWN_HOSTS_TMP"

sudo install -d -o turbgpt -g turbgpt -m 755 /opt/turb-gpt-register
sudo -u turbgpt -H git clone \
  --branch codex/icloud-cloak-docker \
  --single-branch \
  git@github.com:1394805163/turb-gpt-free-register.git \
  /opt/turb-gpt-register
```

如果目录已经存在且不为空，先确认里面是否为本项目，禁止直接覆盖。

## 5. 执行原生安装

```bash
cd /opt/turb-gpt-register
sudo deploy/linux/bootstrap.sh \
  --service-user turbgpt \
  --host 127.0.0.1 \
  --port 5000 \
  --no-start
```

该命令会完成：

1. 安装 Python、Chromium 系统库和编译依赖；
2. 创建 `.venv` 并安装 `requirements.txt`；
3. 以 `turbgpt` 用户安装和检查 CloakBrowser；
4. 验证 CloakBrowser binary 已安装且可以真实启动；
5. 安装并启用 systemd unit，但暂不启动服务。

安装失败时不要反复重跑，先查看终端中的第一条错误。

## 6. 配置 `.env`

```bash
cd /opt/turb-gpt-register
sudo test -f .env || sudo cp .env.example .env
sudo chown turbgpt:turbgpt .env
sudo chmod 600 .env
sudoedit .env
```

至少检查以下配置。所有值均使用自己的真实配置，不要保留尖括号占位符：

```dotenv
WEBUI_AUTH_CODE=<强管理员密码>
WEBUI_SESSION_SECRET=<至少32字节的随机字符串>

REGISTRATION_DRIVER=cloak
CLOAK_HEADLESS=true
CLOAK_KEEP_BROWSER_OPEN=false
CLOAK_LICENSE_KEY=<CloakBrowser授权密钥>

EMAIL_SOURCE=icloud
ICLOUD_MAILBOXES_FILE=data/icloud_mailboxes.txt
ICLOUD_IMAP_HOST=imap.mail.me.com
ICLOUD_IMAP_PORT=993
ICLOUD_IMAP_MAILBOX=INBOX
ICLOUD_IMAP_USERNAME=<iCloud主邮箱>
ICLOUD_IMAP_PASSWORD=<Apple应用专用密码>

REGISTRATION_PROXY_REQUIRED=true
RESIN_MANAGEMENT_URL=<Resin管理地址>
PROXY_POOL_FILE=<代理池文件路径>

CHATGPT2API_PUSH_ENABLED=true
CHATGPT2API_BASE_URL=<chatgpt2api管理地址>
CHATGPT2API_ADMIN_KEY=<chatgpt2api管理员密钥>
```

生成 Session Secret：

```bash
openssl rand -hex 32
```

注意：

- `.env` 必须保持 `0600`，不得提交到 Git；
- iCloud 主邮箱和 Apple 应用专用密码只保存在 `.env`；
- 管理密码、授权密钥和代理凭据不要粘贴到日志或 Issue；
- 注册全流水线并发已在程序中固定为 2，不需要额外设置。

## 7. 导入 iCloud 隐藏邮箱

把隐藏邮箱一行一个写入：

```bash
sudo install -d -o turbgpt -g turbgpt -m 700 /opt/turb-gpt-register/data
sudoedit /opt/turb-gpt-register/data/icloud_mailboxes.txt
sudo chown turbgpt:turbgpt /opt/turb-gpt-register/data/icloud_mailboxes.txt
sudo chmod 600 /opt/turb-gpt-register/data/icloud_mailboxes.txt
```

格式：

```text
alias-one@example.com
alias-two@example.com
```

也可以启动服务后在 WebUI 中导入。不要把主 iCloud 邮箱混入隐藏邮箱池。

## 8. 启动并验收

```bash
sudo systemctl start turb-gpt-register.service
sudo systemctl status turb-gpt-register.service --no-pager
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

检查登录页：

```bash
test "$(curl --silent --output /dev/null \
  --write-out '%{http_code}' \
  http://127.0.0.1:5000/login)" = 200 && echo PASS
```

检查进程和内存：

```bash
systemctl show turb-gpt-register.service \
  -p User -p MainPID -p MemoryCurrent -p MemoryHigh -p TasksCurrent
ps -eo pid,ppid,rss,cmd --sort=-rss | head -20
free -h
```

验收标准：

- `doctor.sh` 全部通过；
- `/login` 返回 HTTP 200；
- systemd 的 `User=turbgpt`，不得为 root；
- Gunicorn 只有 1 个 master 和 1 个 worker；
- 空闲状态没有残留的大量 Chromium 进程；
- 服务停止后 Chromium 子进程随 control group 一起退出。

## 9. 远程访问

默认只监听 `127.0.0.1:5000`，不要直接把 WebUI 暴露到公网。

临时管理推荐使用 SSH 隧道：

```bash
ssh -L 5000:127.0.0.1:5000 <服务器用户>@<服务器IP>
```

然后在本机打开：

```text
http://127.0.0.1:5000/login
```

长期使用时，应配置带 HTTPS 和访问控制的 Nginx 或 Cloudflare Tunnel，再保持应用只监听回环地址。

## 10. 日常运维

```bash
# 状态
sudo systemctl status turb-gpt-register.service --no-pager

# 最近 200 行日志
sudo journalctl -u turb-gpt-register.service -n 200 --no-pager

# 实时日志
sudo journalctl -u turb-gpt-register.service -f

# 重启
sudo systemctl restart turb-gpt-register.service

# 停止
sudo systemctl stop turb-gpt-register.service

# 完整自检
cd /opt/turb-gpt-register
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

## 11. 安全升级

```bash
cd /opt/turb-gpt-register
sudo systemctl stop turb-gpt-register.service

sudo -u turbgpt -H -- cp -a .env \
  .env.backup.$(date +%Y%m%d%H%M%S)

sudo -u turbgpt -H -- git status --short
sudo -u turbgpt -H -- git switch codex/icloud-cloak-docker
sudo -u turbgpt -H -- git pull --ff-only

sudo deploy/linux/bootstrap.sh \
  --service-user turbgpt \
  --host 127.0.0.1 \
  --port 5000 \
  --no-start

sudo systemctl start turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

`git status --short` 非空时先查明原因，不要强制覆盖服务器上的配置或数据。

## 12. 回滚

```bash
cd /opt/turb-gpt-register
sudo systemctl stop turb-gpt-register.service

sudo -u turbgpt -H -- cp -a .env \
  .env.rollback-backup.$(date +%Y%m%d%H%M%S)

sudo -u turbgpt -H -- sh -c \
  'umask 077; tar -czf "/var/lib/turb-gpt-register/runtime-backup.$(date +%Y%m%d%H%M%S).tgz" data logs run'

sudo -u turbgpt -H -- git log -3 --oneline
sudo -u turbgpt -H -- git checkout --detach <确认过的提交哈希>

sudo deploy/linux/bootstrap.sh \
  --service-user turbgpt \
  --host 127.0.0.1 \
  --port 5000 \
  --no-start

sudo systemctl start turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

## 13. 常见故障

### 服务启动失败

```bash
sudo systemctl status turb-gpt-register.service --no-pager
sudo journalctl -u turb-gpt-register.service -n 200 --no-pager
```

### CloakBrowser 启动失败

```bash
cd /opt/turb-gpt-register
sudo -u turbgpt -H -- env \
  HOME=/var/lib/turb-gpt-register \
  XDG_CACHE_HOME=/var/lib/turb-gpt-register/.cache \
  .venv/bin/python -m cloakbrowser doctor --json | \
  python3 deploy/linux/check_cloak_doctor.py
```

### 内存紧张或 OOM

```bash
free -h
swapon --show
systemctl show turb-gpt-register.service \
  -p MemoryCurrent -p MemoryHigh -p TasksCurrent
ps -eo pid,ppid,rss,cmd --sort=-rss | head -20
pgrep -af 'chrom|cloak'
```

保持总并发为 2，不要增加 Gunicorn worker。先停止残留任务或重启服务，再检查 Chromium 是否被正确回收。

### 端口没有响应

```bash
ss -ltnp | grep ':5000'
curl -I http://127.0.0.1:5000/login
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

## 14. 备份清单

至少备份：

- `/opt/turb-gpt-register/.env`；
- `/opt/turb-gpt-register/data/`；
- `/opt/turb-gpt-register/logs/`；
- 当前 Git 分支和提交哈希；
- Resin、chatgpt2api 和反向代理的独立配置。

备份文件同样包含敏感信息，权限应设置为 `0600`，目录设置为 `0700`。
