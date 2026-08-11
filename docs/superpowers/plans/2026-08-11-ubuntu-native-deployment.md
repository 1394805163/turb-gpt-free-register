# Ubuntu 24.04 原生低内存部署实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 为 2 核 2 GB Ubuntu 24.04 LTS VPS 提供可重复执行的原生 Python + Gunicorn + systemd 部署，并保持注册流水线全局并发上限为 2。

**架构：** Web 层使用单 Gunicorn `gthread` worker 和 4 个 HTTP 线程，避免复制进程内队列。systemd 以普通用户运行服务、回收完整浏览器进程组，并用软内存压力线保护主机。CloakBrowser 以无头、临时 profile、任务结束即关闭的方式运行。

**技术栈：** Python 3.12、Flask、Gunicorn、systemd、CloakBrowser、unittest、GitHub Actions Ubuntu 24.04。

**全局约束：** Gunicorn 只能有 1 个 worker；注册流水线硬上限继续为 2；不得设置 Gunicorn `max_requests`；systemd 不得设置 `MemoryMax`；不得添加 Chromium `--no-sandbox`；不得提交 `.env` 或任何凭据。

---

## 文件职责

- 创建 `deploy/linux/gunicorn.conf.py`：唯一的生产 Web 进程配置。
- 创建 `deploy/linux/turb-gpt-register.service.template`：普通用户 systemd 服务模板。
- 创建 `deploy/linux/install-systemd.sh`：安全渲染并安装 unit。
- 创建 `deploy/linux/bootstrap.sh`：Ubuntu 依赖、venv、Cloak 和服务的一键安装。
- 创建 `deploy/linux/doctor.sh`：部署后的只读诊断。
- 创建 `tests/test_linux_deployment.py`：静态部署契约与配置加载测试。
- 创建 `.github/workflows/linux-ci.yml`：Ubuntu 24.04 CI。
- 创建 `.gitattributes`：固定 Linux 文本文件为 LF。
- 创建 `LINUX_DEPLOY.md`：安装、升级、回滚、内存和故障排查。
- 修改 `requirements.txt`：加入 Gunicorn。
- 修改 `webui.sh`：手工启动时优先复用生产 Gunicorn 配置。
- 修改 `README.md`：提供 Linux 部署入口。

### 任务 1：建立单 worker 的低内存 Gunicorn 运行时

**文件：**
- 创建：`deploy/linux/gunicorn.conf.py`
- 修改：`requirements.txt`
- 修改：`webui.sh`
- 测试：`tests/test_linux_deployment.py`

- [ ] **步骤 1：编写失败的 Gunicorn 契约测试**

```python
class GunicornConfigTests(unittest.TestCase):
    def test_single_worker_gthread_runtime(self):
        cfg = runpy.run_path(str(ROOT / "deploy/linux/gunicorn.conf.py"))
        self.assertEqual(cfg["workers"], 1)
        self.assertEqual(cfg["worker_class"], "gthread")
        self.assertEqual(cfg["threads"], 4)
        self.assertFalse(cfg["preload_app"])
        self.assertNotIn("max_requests", cfg)

    def test_runtime_dependency_contains_gunicorn(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8-sig")
        self.assertRegex(requirements, r"(?m)^gunicorn>=")
```

- [ ] **步骤 2：运行测试并确认因文件和依赖缺失而失败**

运行：`.venv/Scripts/python.exe -m unittest tests.test_linux_deployment -v`

预期：FAIL，指出 `gunicorn.conf.py` 不存在且 requirements 缺少 Gunicorn。

- [ ] **步骤 3：实现最小生产配置**

`gunicorn.conf.py` 固定 1 worker、`gthread`、4 threads、`preload_app=False`、`graceful_timeout=45`，绑定地址从 `HOST`/`PORT` 环境变量读取；不要定义 `max_requests`。

`webui.sh` 在 `.venv/bin/gunicorn` 存在时启动：

```bash
"$ROOT_DIR/.venv/bin/gunicorn" \
  --config "$ROOT_DIR/deploy/linux/gunicorn.conf.py" \
  "webui.app:create_app()"
```

没有 Gunicorn 时保留 `python web.py` 回退。PID 查找同时识别 Gunicorn 和旧入口。

- [ ] **步骤 4：运行定向测试并确认通过**

运行：`.venv/Scripts/python.exe -m unittest tests.test_linux_deployment.GunicornConfigTests -v`

预期：全部 PASS。

- [ ] **步骤 5：提交任务 1**

```bash
git add deploy/linux/gunicorn.conf.py requirements.txt webui.sh tests/test_linux_deployment.py
git commit -m "perf: 添加单进程低内存 Gunicorn 运行时"
```

### 任务 2：实现 systemd 与可重复执行的 Ubuntu 安装器

**文件：**
- 创建：`deploy/linux/turb-gpt-register.service.template`
- 创建：`deploy/linux/install-systemd.sh`
- 创建：`deploy/linux/bootstrap.sh`
- 创建：`deploy/linux/doctor.sh`
- 修改：`tests/test_linux_deployment.py`

- [ ] **步骤 1：编写失败的 systemd 和安装器契约测试**

```python
def test_systemd_memory_and_process_contract(self):
    unit = read("deploy/linux/turb-gpt-register.service.template")
    self.assertIn("KillMode=control-group", unit)
    self.assertIn("MemoryHigh=1700M", unit)
    self.assertNotIn("MemoryMax=", unit)
    self.assertIn("MALLOC_ARENA_MAX=2", unit)
    self.assertIn("OOMPolicy=continue", unit)

def test_bootstrap_preserves_secrets_and_installs_cloak_as_service_user(self):
    script = read("deploy/linux/bootstrap.sh")
    self.assertIn("if [[ ! -e \"$APP_DIR/.env\" ]]", script)
    self.assertIn("cloakbrowser install", script)
    self.assertIn("run_as_service_user", script)
    self.assertNotIn("--no-sandbox", script)
```

同时验证模板包含非 root `User`、`Group`、`EnvironmentFile`、`Restart=on-failure`、`TimeoutStopSec=45` 和 `TasksMax=512`。

- [ ] **步骤 2：运行测试并确认因部署文件缺失而失败**

运行：`.venv/Scripts/python.exe -m unittest tests.test_linux_deployment -v`

预期：FAIL，指出 systemd、bootstrap 和 doctor 文件不存在。

- [ ] **步骤 3：实现 systemd 安装器**

`install-systemd.sh` 接受 `--service-user`、`--host`、`--port`、`--no-start`。它必须：

- 拒绝 root 作为最终服务用户；
- 验证项目绝对路径不含换行；
- 用转义后的真实路径、用户和 HOME 渲染模板；
- 将 unit 原子写入 `/etc/systemd/system/turb-gpt-register.service`；
- 执行 `systemctl daemon-reload`，默认 `enable --now`；
- 不读取或输出 `.env` 内容。

- [ ] **步骤 4：实现 Ubuntu bootstrap**

`bootstrap.sh` 必须：

- 检查 Ubuntu 24.04，并对其他版本给出警告而非静默继续；
- 仅支持 `x86_64` 和 `aarch64`，对 ARM64 输出 Cloak 免费 binary 兼容提示；
- root 直接执行时创建 `turbgpt` 普通系统用户；
- 安装 `python3`、`python3-venv`、`python3-pip`、`curl`、`ca-certificates`；
- 创建 `.venv` 并安装 requirements；
- 执行 `python -m playwright install-deps chromium`，只安装系统库，不下载 Playwright Chromium；
- 以服务用户和其 HOME 执行 `python -m cloakbrowser install`、`doctor --quick`；
- 只在 `.env` 不存在时复制 `.env.example`，并设置 `0600`；
- 创建 `logs`、`run`、`data` 并交给服务用户；
- 调用 `install-systemd.sh`；
- 重复执行不会覆盖配置或数据。

- [ ] **步骤 5：实现只读 doctor**

`doctor.sh` 检查架构、venv、`.env`、Cloak binary、systemd active 状态、端口 `/login` 和 cgroup 内存。任何检查不得输出密钥或 `.env` 内容。

- [ ] **步骤 6：运行定向测试和 shell 语法检查**

运行：

```bash
.venv/Scripts/python.exe -m unittest tests.test_linux_deployment -v
"C:/Program Files/Git/bin/bash.exe" -n deploy/linux/bootstrap.sh
"C:/Program Files/Git/bin/bash.exe" -n deploy/linux/install-systemd.sh
"C:/Program Files/Git/bin/bash.exe" -n deploy/linux/doctor.sh
"C:/Program Files/Git/bin/bash.exe" -n webui.sh
```

预期：全部退出 0。

- [ ] **步骤 7：提交任务 2**

```bash
git add deploy/linux tests/test_linux_deployment.py
git commit -m "feat: 添加 Ubuntu systemd 一键部署"
```

### 任务 3：补齐 Linux 文档、LF 规则和 Ubuntu CI

**文件：**
- 创建：`LINUX_DEPLOY.md`
- 创建：`.gitattributes`
- 创建：`.github/workflows/linux-ci.yml`
- 修改：`README.md`
- 修改：`tests/test_linux_deployment.py`

- [ ] **步骤 1：编写失败的文档与 CI 契约测试**

```python
def test_ci_targets_ubuntu_2404(self):
    workflow = read(".github/workflows/linux-ci.yml")
    self.assertIn("runs-on: ubuntu-24.04", workflow)
    self.assertIn("python -m unittest discover -s tests -v", workflow)

def test_linux_docs_cover_two_gib_operations(self):
    docs = read("LINUX_DEPLOY.md")
    for phrase in ("2 核 2 GB", "1 个 Gunicorn worker", "并发上限为 2", "swap", "journalctl"):
        self.assertIn(phrase, docs)
```

- [ ] **步骤 2：运行测试并确认因文档和 CI 缺失而失败**

运行：`.venv/Scripts/python.exe -m unittest tests.test_linux_deployment -v`

预期：FAIL，指出 Linux 文档、LF 规则和 workflow 缺失。

- [ ] **步骤 3：编写部署与运维文档**

文档提供可直接运行的 `/opt` 安装命令，并覆盖：

- 首次部署和 `.env` 填写；
- 默认仅监听 `127.0.0.1`；
- 可选 1–2 GB swap；
- 启停、日志、状态和 doctor；
- `git pull --ff-only` 升级；
- 回滚到上一提交；
- RSS/cgroup/Chromium 进程检查；
- OOM、Cloak 启动失败和端口不可达排查；
- 2C2G 只能保持总并发 2，不建议提高。

- [ ] **步骤 4：添加 LF 规则和 Ubuntu CI**

`.gitattributes` 至少包含：

```gitattributes
*.sh text eol=lf
*.service text eol=lf
*.template text eol=lf
*.yml text eol=lf
*.yaml text eol=lf
```

CI 使用 `ubuntu-24.04` 和 Python 3.12，安装 requirements，执行 `bash -n`、Linux 部署测试、`compileall` 和完整 unittest。CI 不下载 Cloak binary，也不需要任何密钥。

- [ ] **步骤 5：运行定向测试并确认通过**

运行：`.venv/Scripts/python.exe -m unittest tests.test_linux_deployment -v`

预期：全部 PASS。

- [ ] **步骤 6：提交任务 3**

```bash
git add LINUX_DEPLOY.md README.md .gitattributes .github/workflows/linux-ci.yml tests/test_linux_deployment.py
git commit -m "docs: 补齐 Ubuntu 部署文档与 CI"
```

### 任务 4：执行全量验收和最终复审

**文件：**
- 可能修改：前述文件中由验收发现的问题

- [ ] **步骤 1：执行完整静态和单元测试**

```powershell
.venv\Scripts\python.exe -m compileall -q core webui config web.py main.py
.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

预期：compileall 退出 0；全部测试 0 failures；diff check 无输出。

- [ ] **步骤 2：执行 Gunicorn 应用工厂冒烟**

在 Linux 容器或 Ubuntu CI 等价环境中启动单 worker Gunicorn，访问 `/login`，确认 HTTP 200，然后正常发送 SIGTERM 并检查无遗留子进程。

- [ ] **步骤 3：执行安全和配置扫描**

```bash
git grep -n -E '(sk-[A-Za-z0-9]{16,}|npg_[A-Za-z0-9]+|ICLOUD_IMAP_PASSWORD=.+|CHATGPT2API_ADMIN_KEY=.+)'
git grep -n -- '--no-sandbox'
```

预期：没有真实凭据；没有新增 `--no-sandbox`。

- [ ] **步骤 4：核对内存与并发约束**

- Gunicorn worker 为 1；
- HTTP threads 为 4；
- `PIPELINE_MAX_CONCURRENCY` 为 2；
- `MemoryHigh=1700M` 且不存在 `MemoryMax`；
- Cloak 默认 headless 且任务结束关闭；
- systemd `KillMode=control-group`。

- [ ] **步骤 5：执行最终代码审查**

审查范围从设计提交前的基线到当前 HEAD。Critical、Important、Minor 均必须为 0；发现问题后修复并重新运行对应验证。

- [ ] **步骤 6：提交验收修复并推送**

```bash
git push fork codex/icloud-cloak-docker
```

推送后用 `git ls-remote` 核对远端分支 HEAD 与本地一致。

