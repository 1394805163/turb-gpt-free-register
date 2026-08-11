# Ubuntu 24.04 原生低内存部署设计

## 目标

将注册机部署为 Ubuntu 24.04 LTS 原生服务，不使用 Docker。在 2 核 2 GB 内存的 x86_64 VPS 上，保留最多 2 条注册流水线并发，同时降低常驻内存、浏览器残留和服务重复初始化风险。

## 方案选择

采用以下组合：

- Python 虚拟环境负责依赖隔离。
- Gunicorn 负责生产级 HTTP 服务。
- systemd 负责开机启动、异常重启、日志和浏览器子进程回收。
- CloakBrowser 使用 Linux x64 无头二进制，每个任务结束立即关闭临时浏览器上下文。
- 注册、套餐查询、测活和推送继续共用代码内的全局并发门禁，峰值固定为 2。

不使用 Docker，避免同时常驻容器运行时和镜像层。也不使用多个 Gunicorn worker，因为应用的队列、锁和线程池均为进程内状态；多 worker 会复制这些状态，并让全局并发上限失效。

## 进程模型

Gunicorn 固定使用以下模型：

- `workers = 1`
- `worker_class = "gthread"`
- `threads = 4`
- 不启用 `preload_app`
- 不设置 `max_requests` 自动回收

HTTP 线程只负责 WebUI 和任务入队。耗时注册工作仍由项目现有后台线程池执行。4 个 HTTP 线程不会把注册并发提高到 4；`core/pipeline_concurrency.py` 中的硬门禁仍将注册流水线总并发限制为 2。

`max_requests` 会在后台注册任务运行时替换 worker，可能中断任务，因此明确禁用。Gunicorn 只运行一个 worker，也避免 `create_app()` 重复恢复任务和创建多套线程池。

## 内存控制

### Python 进程

- 设置 `MALLOC_ARENA_MAX=2`，减少多线程下 glibc malloc arena 的内存碎片。
- 设置 `PYTHONUNBUFFERED=1`，让日志直接交给 journald，不额外维护文件日志缓冲。
- Gunicorn 只启一个 worker，避免复制 Flask、数据库状态和线程池。
- 不开启调试模式和自动重载。

### CloakBrowser

- 默认 `CLOAK_HEADLESS=true`。
- 默认 `CLOAK_KEEP_BROWSER_OPEN=false`。
- `CLOAK_USER_DATA_DIR` 默认留空，使用任务级临时 profile，任务结束后随浏览器关闭。
- 服务必须以普通用户运行，不能为了兼容而添加 `--no-sandbox`。
- 同时最多运行 2 个浏览器会话，与全局流水线硬上限一致。

### systemd 资源策略

- `MemoryHigh=1700M`：作为软压力线，接近上限时优先回收缓存，不直接杀死服务。
- 不设置 `MemoryMax`：两个浏览器短时峰值可能超过估算，硬上限会制造无提示中断。
- `OOMPolicy=continue`：单个浏览器子进程被内核终止时保留 WebUI，由任务层报告失败。
- `KillMode=control-group`：停止或重启服务时清理 Gunicorn 和全部浏览器子进程。
- `TasksMax=512`：允许 Chromium 多进程，但避免失控创建进程。

2 GB 主机建议额外配置 1–2 GB swap 处理短时峰值。swap 只作为保险，不代替内存；安装脚本不默认改动磁盘，文档提供显式命令。

## 安装与运行用户

服务使用专用普通用户运行。安装脚本按以下优先级选择用户：

1. 显式 `--service-user`；
2. `sudo` 调用者；
3. 当前普通用户；
4. root 直接执行时创建 `turbgpt` 系统用户。

项目目录必须可被该用户遍历和写入。推荐安装到 `/opt/turb-gpt-free-register`，运行用户的 HOME 使用 `/var/lib/turb-gpt-register`，以便 CloakBrowser 保存自身缓存和 Linux binary。

敏感配置只放在 `.env`。安装脚本只在文件不存在时从 `.env.example` 创建 `.env`，绝不覆盖已有值，也不通过 systemd 的 `ExecStart` 参数暴露授权码。

## 部署文件

新增以下文件：

- `deploy/linux/bootstrap.sh`：安装 Ubuntu 依赖、创建虚拟环境、安装 Python 包和 CloakBrowser，并调用 systemd 安装器。
- `deploy/linux/install-systemd.sh`：渲染 unit 模板、执行 daemon-reload，并按参数启动服务。
- `deploy/linux/turb-gpt-register.service.template`：systemd 服务模板。
- `deploy/linux/gunicorn.conf.py`：单 worker、gthread 和日志配置。
- `deploy/linux/doctor.sh`：检查架构、Python、配置、Cloak binary、服务和内存。
- `LINUX_DEPLOY.md`：Ubuntu 24.04 安装、升级、回滚、swap 与故障排查说明。
- `.gitattributes`：强制 shell 和 systemd 文件使用 LF。
- `.github/workflows/linux-ci.yml`：在 Ubuntu 24.04 上执行部署结构与项目测试。

`requirements.txt` 增加 Gunicorn。`webui.sh` 在虚拟环境存在 Gunicorn 时使用同一份生产配置；没有 Gunicorn 时才回退到 Flask 开发服务器，保持旧开发环境可用。

## 安装流程

推荐流程：

1. 在 `/opt/turb-gpt-free-register` 克隆指定分支。
2. 运行 `sudo bash deploy/linux/bootstrap.sh`。
3. bootstrap 安装 Python、venv 和浏览器系统依赖。
4. 创建 `.venv`，安装 `requirements.txt`。
5. 以服务用户执行 `python -m cloakbrowser install` 和 `doctor`。
6. 创建但不覆盖 `.env`。
7. 安装并启动 `turb-gpt-register.service`。
8. 通过本机 `/login` 和 `systemctl status` 验收。

安装脚本必须可重复执行。再次执行时复用 `.env`、数据库和账号数据，只更新依赖、Cloak binary 与 systemd unit。

## 升级与停止语义

升级前先停止服务，避免 Git 更新或依赖替换时仍有注册任务运行：

1. `systemctl stop turb-gpt-register`
2. `git pull --ff-only`
3. 重新运行 bootstrap
4. `systemctl start turb-gpt-register`

systemd 停止时先向 Gunicorn 发送 SIGTERM，并给后台任务最多 45 秒收尾；随后按 control group 清理遗留浏览器。被中断任务依赖现有数据库恢复逻辑从 `queued/running` 回到可重试状态。

## 安全边界

- WebUI 默认只监听 `127.0.0.1:5000`。
- 对公网开放时使用 Nginx 或 Cloudflare Tunnel 终止 TLS，不直接暴露 Gunicorn。
- 服务以普通用户运行，`.env` 权限为 `0600`，运行目录权限为 `0700`。
- `.env`、账号、Token、邮箱密码和 Cloak license 均不进入 Git。
- 不向 Chromium 添加 `--no-sandbox`。

## 验收标准

### 自动化

- Linux 部署测试验证 Gunicorn 固定单 worker、4 threads、无自动回收。
- Linux 部署测试验证 systemd 使用普通用户、control-group 清理、软内存线且没有硬 `MemoryMax`。
- Linux 部署测试验证 bootstrap 不覆盖 `.env`，并以服务用户安装 CloakBrowser。
- shell 文件通过 `bash -n`。
- Python 文件通过 `compileall`。
- 完整 unittest 继续全部通过。
- Ubuntu 24.04 GitHub Actions 可安装依赖并执行部署定向测试。

### VPS 验收

- `systemctl is-active turb-gpt-register` 返回 `active`。
- `curl -I http://127.0.0.1:5000/login` 返回 200。
- `python -m cloakbrowser doctor` 能启动 Linux x64 binary。
- 空闲时只有一套 Gunicorn worker 和项目线程池。
- 同时提交 2 个任务时峰值并发不超过 2；第 3 个任务等待，不创建第 3 个浏览器会话。
- 重启服务后没有遗留 Cloak/Chromium 进程。
