# Ubuntu 原生部署与运维

本项目在 Ubuntu 24.04 上使用 systemd 运行 WebUI。推荐 2 核 2 GB 主机；服务配置为 1 个 Gunicorn worker、4 个 gthread，应用并发上限为 2。2 GB 机器请配置 1–2 GB swap，并观察 cgroup/RSS 与 Chromium 子进程，不要在内存紧张时提高并发。

## 首次部署

将仓库放到 `/opt/turb-gpt-register`，准备好 `.env.example` 后执行：

```bash
cd /opt/turb-gpt-register
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000
```

`bootstrap.sh` 的参数只有 `--service-user USER`、`--host HOST`、`--port PORT`、`--no-start`、`--print-service-user` 和 `--help`。默认监听 `127.0.0.1:5000`；不传 `--service-user` 时优先使用 `SUDO_USER`，否则使用当前普通用户，root 直接执行时创建 `turbgpt`。脚本会安装 Python 系统依赖、创建 `.venv`、安装 `requirements.txt`、安装 Chromium 系统库、以服务用户执行 Cloak 安装与快速检查，并生成 systemd 服务。

首次运行会在 `.env` 不存在时从 `.env.example` 创建 `.env`，之后保留现有 `.env`。部署前编辑密钥、WebUI 认证码和驱动配置：

```bash
sudoedit /opt/turb-gpt-register/.env
sudo chmod 600 /opt/turb-gpt-register/.env
```

需要先准备 swap 时（按主机剩余磁盘空间选择 1–2 GB）：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
printf '/swapfile none swap sw 0 0\n' | sudo tee -a /etc/fstab
```

不启动服务安装：

```bash
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
```

## 手动安装或重渲染 systemd

`install-systemd.sh` 支持以下实际参数：`--service-user`、`--service-group`、`--service-home`、`--app-dir`、`--host`、`--port`、`--no-start`、`--render-only FILE`、`--check-access-only`、`--apply-unit-only` 和 `--help`。完整安装示例：

```bash
sudo deploy/linux/install-systemd.sh --app-dir /opt/turb-gpt-register \
  --service-user turbgpt --service-group turbgpt --service-home /home/turbgpt \
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

`doctor.sh` 会检查架构、`.venv/bin/python`、`.env`、Cloak binary、systemd 状态、`/login` 和 cgroup memory。它需要在已安装服务的主机上运行；端口或 `/login` 不可达时先看 `journalctl`。

## 升级与回滚

升级前保留 `.env` 备份，并确认工作树没有未提交修改：

```bash
cd /opt/turb-gpt-register
sudo cp -a .env .env.backup.$(date +%Y%m%d%H%M%S)
git status --short
git pull --ff-only
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
sudo systemctl restart turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

回滚到上一个提交：

```bash
cd /opt/turb-gpt-register
git log -2 --oneline
git checkout --detach HEAD^
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
sudo systemctl restart turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

确认问题已定位后回到分支并重新执行 `git pull --ff-only`。若升级包含 systemd 模板变化，必须按以下顺序重新安装 unit、重启服务并检查；`--no-start` 只完成 unit 安装，不会让已运行进程立即采用新 unit：

```bash
sudo deploy/linux/bootstrap.sh --service-user turbgpt --host 127.0.0.1 --port 5000 --no-start
sudo systemctl restart turb-gpt-register.service
sudo deploy/linux/doctor.sh --host 127.0.0.1 --port 5000
```

## 2C2G 排查清单

- **OOM 或频繁重启**：查看 `journalctl -u turb-gpt-register.service`、`systemctl status`、`free -h`、`swapon --show`，以及 `/sys/fs/cgroup/system.slice/turb-gpt-register.service/memory.current`（实际 cgroup 路径以 `systemctl show -p ControlGroup` 为准）。unit 设置 `MemoryHigh=1700M`、`TasksMax=512`、`MALLOC_ARENA_MAX=2`；2C2G 保持总并发为 2，不建议提高。
- **Cloak 启动失败**：确认 `sudo -u turbgpt -H /opt/turb-gpt-register/.venv/bin/python -m cloakbrowser doctor --quick` 的输出、服务用户 HOME、`.venv/bin/cloakbrowser` 权限及 Chromium 系统库；随后查看完整 journal。CI 不执行 binary 安装。
- **端口不可达**：确认 unit 的 `Environment="HOST=..."` 与 `Environment="PORT=..."`，运行 `ss -ltnp | grep ':5000'`、`curl --fail http://127.0.0.1:5000/login`，再看代理、防火墙和 `doctor.sh`。
- **Chromium 进程过多**：检查 `pgrep -af 'chrom|cloak'`、`ps -eo pid,ppid,rss,cmd --sort=-rss | head`，停止残留任务后再重启服务；不要用增加 worker 的方式处理。

## 安全与备份

`.env` 含密钥，保持 `0600`，不要提交到 Git。默认 `127.0.0.1` 只允许本机访问；远程管理通过 SSH 隧道或已认证反向代理。定期备份 `.env`、`data/`、`logs/` 与 Git 提交信息，并在回滚前确认备份可读。
