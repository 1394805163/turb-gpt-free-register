#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
SERVICE_USER="turbgpt"
HOST="127.0.0.1"
PORT="5000"
NO_START=0

usage() {
  cat <<'EOF'
用法: sudo deploy/linux/bootstrap.sh [选项]

选项:
  --service-user USER  运行服务的普通用户（默认: turbgpt）
  --host HOST          WebUI 监听地址（默认: 127.0.0.1）
  --port PORT          WebUI 监听端口（默认: 5000）
  --no-start           安装后不启动服务
  -h, --help           显示帮助
EOF
}

fail() {
  printf '错误: %s\n' "$*" >&2
  exit 1
}

require_value() {
  [[ $# -ge 2 && -n "$2" ]] || fail "$1 需要一个值"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-user)
      require_value "$1" "${2:-}"
      SERVICE_USER="$2"
      shift 2
      ;;
    --host)
      require_value "$1" "${2:-}"
      HOST="$2"
      shift 2
      ;;
    --port)
      require_value "$1" "${2:-}"
      PORT="$2"
      shift 2
      ;;
    --no-start)
      NO_START=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "未知选项: $1"
      ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "请以 root 身份运行 bootstrap 脚本"
[[ "$APP_DIR" == /* ]] || fail "项目路径必须是绝对路径"
[[ "$APP_DIR" != *$'\n'* && "$APP_DIR" != *$'\r'* ]] || fail "项目路径不能包含换行符"
[[ "$SERVICE_USER" != "root" ]] || fail "root 不能作为最终服务用户"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || fail "端口必须在 1 到 65535 之间"
[[ -f "$APP_DIR/requirements.txt" ]] || fail "找不到 requirements.txt"
[[ -f "$APP_DIR/.env.example" ]] || fail "找不到 .env.example"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    printf '警告: 当前系统不是 Ubuntu 24.04（检测到: %s %s）；将继续执行，但请自行验证依赖兼容性。\n' \
      "${ID:-unknown}" "${VERSION_ID:-unknown}" >&2
  fi
else
  printf '警告: 无法读取 /etc/os-release；将继续执行，但请自行验证系统兼容性。\n' >&2
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)
    ;;
  aarch64)
    printf '提示: ARM64 使用 Cloak 免费 binary 前，请确认当前版本的兼容性。\n' >&2
    ;;
  *)
    fail "仅支持 x86_64 和 aarch64，当前架构: $ARCH"
    ;;
esac

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[[ "$SERVICE_HOME" == /* ]] || fail "服务用户 HOME 必须是绝对路径"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SERVICE_HOME"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip curl ca-certificates

VENV_DIR="$APP_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  python3 -m venv "$VENV_DIR"
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$VENV_DIR"

run_as_service_user() {
  runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"
}

run_as_service_user "$VENV_PYTHON" -m pip install --upgrade pip
run_as_service_user "$VENV_PYTHON" -m pip install -r "$APP_DIR/requirements.txt"

# install-deps 只安装 Chromium 所需的系统库，不下载 Playwright Chromium。
"$VENV_PYTHON" -m playwright install-deps chromium

install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
  "$APP_DIR/logs" "$APP_DIR/run" "$APP_DIR/data"

if [[ ! -e "$APP_DIR/.env" ]]; then
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
    "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

run_as_service_user "$VENV_PYTHON" -m cloakbrowser install
run_as_service_user "$VENV_PYTHON" -m cloakbrowser doctor --quick

INSTALL_ARGS=(--service-user "$SERVICE_USER" --host "$HOST" --port "$PORT")
if [[ "$NO_START" -eq 1 ]]; then
  INSTALL_ARGS+=(--no-start)
fi
"$SCRIPT_DIR/install-systemd.sh" "${INSTALL_ARGS[@]}"

printf 'Ubuntu 原生部署完成。\n'
