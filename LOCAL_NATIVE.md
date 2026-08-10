# Windows 原生运行

本目录使用 Python 3.12 独立虚拟环境运行，不需要 Docker，也不会调用系统全局 Python 依赖。

## 一键使用

按顺序双击：

1. `01-init.bat`：创建 `.venv`、安装依赖并生成本地 `.env`。
2. `02-start.bat`：后台启动 WebUI。
3. `04-status.bat`：查看进程、HTTP 健康状态和代理文件状态。
4. `03-stop.bat`：停止本项目 WebUI。

WebUI 固定地址：<http://127.0.0.1:5000/>

初始化生成的授权码是占位值 `CHANGE_ME_LOCAL_ONLY`。它只适合当前电脑上的首次启动，请修改 `.env` 中的 `WEBUI_AUTH_CODE`。WebUI 固定绑定 `127.0.0.1`，不会监听局域网地址。

## PowerShell

```powershell
.\manage.ps1 -Action init
.\manage.ps1 -Action start
.\manage.ps1 -Action status
.\manage.ps1 -Action health
.\manage.ps1 -Action stop
```

WebUI 后台日志位于：

- `logs/webui.stdout.log`
- `logs/webui.stderr.log`

PID 位于 `run/webui.pid`。脚本停止进程前会核对命令行，避免 PID 被系统复用后误停其他程序。

## 浏览器驱动

本地配置使用 CloakBrowser 无头模式：

```dotenv
REGISTRATION_DRIVER=cloak
CLOAK_HEADLESS=True
CLOAK_KEEP_BROWSER_OPEN=False
```

启动 WebUI 不会启动注册任务。只有在 WebUI 中主动提交任务后，才会创建 Cloak 浏览器会话。

## 代理来源

默认只读以下文件：

```text
../runtime/resin/data/register-proxies.txt
```

文件存在且包含有效代理时优先使用；文件不存在或为空时回退到 `.env` 的 `PROXY_POOL`。支持：

```text
IP:PORT
http://HOST:PORT
https://USER:PASSWORD@HOST:PORT
socks5://HOST:PORT
socks5h://USER:PASSWORD@HOST:PORT
```

该文件由 Resin 一键配置和严格验证流程生成。每一行都有独立的 `Platform.Account` 身份；注册任务选中一行后，整次任务保持同一 Resin Account 和出口。裸 `IP:PORT` 仍会按 `http://IP:PORT` 导入，空行、注释、重复项和不支持的协议会被忽略。
