# Ubuntu 原生部署与运维

本项目在 Ubuntu 24.04 上使用 systemd 运行 WebUI。推荐 2 核 2 GB 主机；服务配置为 1 个 Gunicorn worker、4 个 gthread，应用并发上限为 2。2 GB 机器请配置 1–2 GB swap，并观察 cgroup/RSS 与 Chromium 子进程，不要在内存紧张时提高并发。

## 首次部署

将仓库放到 `/opt/turb-gpt-register`，准备好 `.env.example` 后执行：

```bash
cd /opt/turb-gpt-register
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
```

`bootstrap.sh` 支持 `--service-user USER`、`--service-home HOME`、`--host HOST`、`--port PORT`、`--no-start`、`--print-service-user` 和 `--help`。默认监听 `127.0.0.1:5000`，服务状态 HOME 固定为 `/var/lib/turb-gpt-register`；只有明确需要时才用 `--service-home` 覆盖。不传 `--service-user` 时优先使用 `SUDO_USER`，否则使用当前普通用户；root 直接执行时选择并创建 `turbgpt`。显式选择尚不存在的 `turbgpt` 也会创建该系统账号，其他不存在的显式用户会报错。脚本不会改写交互用户原有 HOME。

脚本会安装 Python 系统依赖、创建 `.venv`、安装 `requirements.txt`、安装 Chromium 系统库，并以服务用户及状态 HOME 执行 Cloak 安装和完整 `python -m cloakbrowser doctor`（包含真实 binary 启动），最后生成 systemd 服务。`--no-start` 仍会安装并 enable unit，但不会启动或重启服务。

首次运行会在 `.env` 不存在时从 `.env.example` 创建 `.env`，之后保留现有 `.env`。部署前编辑密钥、WebUI 认证码和驱动配置：

```bash
sudoedit /opt/turb-gpt-register/.env
sudo chmod 600 /opt/turb-gpt-register/.env
sudo systemctl start turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

需要先准备 swap 时（按主机剩余磁盘空间选择 1–2 GB）：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
printf '/swapfile none swap sw 0 0\n' | sudo tee -a /etc/fstab
```

`/var/lib/turb-gpt-register` 与项目下的 `logs/`、`run/`、`data/` 均由服务用户所有且权限为 `0700`；unit 的 `HOME` 指向该状态目录，Cloak 缓存、license 和 Linux binary 不会写入交互用户 HOME。

## 手动安装或重渲染 systemd

`install-systemd.sh` 支持以下实际参数：`--service-user`、`--service-group`、`--service-home`、`--app-dir`、`--host`、`--port`、`--no-start`、`--render-only FILE`、`--check-access-only`、`--apply-unit-only` 和 `--help`。完整安装示例：

```bash
sudo deploy/linux/install-systemd.sh --app-dir /opt/turb-gpt-register \
  --service-user turbgpt --service-group turbgpt --service-home /var/lib/turb-gpt-register \
  --host 127.0.0.1 --port 5000
```

默认仅监听回环地址。要通过反向代理或内网暴露时，明确传入 `--host`，并在防火墙和代理层完成访问控制；不要把 WebUI 直接暴露到公网。

## 启停、日志与状态

```bash
sudo systemctl start turb-gpt-register.service
sudo systemctl stop turb-gpt-register.service
sudo systemctl restart turb-gpt-register.service
sudo systemctl status turb-gpt-register.service --no-pager
sudo journalctl -u turb-gpt-register.service -n 200 --no-pager
sudo journalctl -u turb-gpt-register.service -f
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

`doctor.sh` 会以 unit 中的服务用户和 `/var/lib/turb-gpt-register` HOME 执行完整 `python -m cloakbrowser doctor`，实际启动 Cloak binary，并检查架构、`.venv/bin/python`、`.env`、systemd 状态、`/login` 精确 HTTP 200 和 cgroup memory。它需要在已安装服务的主机上运行；端口或 `/login` 不可达时先看 `journalctl`。

## 升级与回滚

升级严格按“停止 → 以仓库所有者/服务用户备份并执行 Git → bootstrap `--no-start` → 启动 → doctor”执行。`/opt` 首装后仓库归 `turbgpt`，因此不要用 root 或其他交互用户直接运行 Git：

```bash
cd /opt/turb-gpt-register
sudo systemctl stop turb-gpt-register.service
sudo -u turbgpt -- cp -a .env .env.backup.$(date +%Y%m%d%H%M%S)
sudo -u turbgpt -- git -C /opt/turb-gpt-register status --short
sudo -u turbgpt -- git -C /opt/turb-gpt-register pull --ff-only
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
sudo systemctl start turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

回滚到上一个提交时同样先停止服务；`.env` 和运行数据备份也以拥有这些 `0600/0700` 文件的服务用户执行，Git 操作始终以仓库所有者执行：

```bash
cd /opt/turb-gpt-register
sudo systemctl stop turb-gpt-register.service
sudo -u turbgpt -- cp -a .env .env.rollback-backup.$(date +%Y%m%d%H%M%S)
sudo -u turbgpt -- tar -C /opt/turb-gpt-register -czf /var/lib/turb-gpt-register/runtime-backup.$(date +%Y%m%d%H%M%S).tgz data logs run
sudo -u turbgpt -- git -C /opt/turb-gpt-register log -2 --oneline
sudo -u turbgpt -- git -C /opt/turb-gpt-register checkout --detach HEAD^
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
sudo systemctl start turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

确认问题已定位后，仍按停止服务、以 `turbgpt` 回到分支并执行 `git pull --ff-only`、重新安装 unit、启动和检查的顺序操作；`--no-start` 不会启动或重启服务：

```bash
sudo systemctl stop turb-gpt-register.service
sudo -u turbgpt -- git -C /opt/turb-gpt-register switch codex/icloud-cloak-docker
sudo -u turbgpt -- git -C /opt/turb-gpt-register pull --ff-only
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
sudo systemctl start turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

## 2C2G 排查清单

- **OOM 或频繁重启**：查看 `journalctl -u turb-gpt-register.service`、`systemctl status`、`free -h`、`swapon --show`，以及 `/sys/fs/cgroup/system.slice/turb-gpt-register.service/memory.current`（实际 cgroup 路径以 `systemctl show -p ControlGroup` 为准）。unit 设置 `MemoryHigh=1700M`、`TasksMax=512`、`MALLOC_ARENA_MAX=2`；2C2G 保持总并发为 2，不建议提高。
- **Cloak 启动失败**：确认 `sudo -u turbgpt -- env HOME=/var/lib/turb-gpt-register XDG_CACHE_HOME=/var/lib/turb-gpt-register/.cache /opt/turb-gpt-register/.venv/bin/python -m cloakbrowser doctor` 能实际启动 binary，并核对状态 HOME 与 Chromium 系统库；随后查看完整 journal。CI 不执行 binary 安装。
- **端口不可达**：确认 unit 的 `Environment="HOST=..."` 与 `Environment="PORT=..."`，运行 `ss -ltnp | grep ':5000'` 和 `test "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:5000/login)" = 200`，再看代理、防火墙和 `doctor.sh`。
- **Chromium 进程过多**：检查 `pgrep -af 'chrom|cloak'`、`ps -eo pid,ppid,rss,cmd --sort=-rss | head`，停止残留任务后再重启服务；不要用增加 worker 的方式处理。

## 安全与备份

`.env` 含密钥，保持 `0600`，不要提交到 Git。默认 `127.0.0.1` 只允许本机访问；远程管理通过 SSH 隧道或已认证反向代理。定期备份 `.env`、`data/`、`logs/` 与 Git 提交信息，并在回滚前确认备份可读。
