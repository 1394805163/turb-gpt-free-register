#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="turb-gpt-register"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
TEMPLATE="$SCRIPT_DIR/${SERVICE_NAME}.service.template"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="turbgpt"
HOST="127.0.0.1"
PORT="5000"
START_SERVICE=1

usage() {
  cat <<'EOF'
用法: sudo deploy/linux/install-systemd.sh [选项]

选项:
  --service-user USER  运行服务的普通用户（默认: turbgpt）
  --host HOST          WebUI 监听地址（默认: 127.0.0.1）
  --port PORT          WebUI 监听端口（默认: 5000）
  --no-start           只安装并启用服务，不立即启动
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

validate_no_newline() {
  local label="$1"
  local value="$2"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "$label 不能包含换行符"
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
      START_SERVICE=0
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

[[ "${EUID}" -eq 0 ]] || fail "必须以 root 身份安装 systemd 服务"
[[ "$APP_DIR" == /* ]] || fail "项目路径必须是绝对路径"
validate_no_newline "项目路径" "$APP_DIR"
validate_no_newline "服务用户" "$SERVICE_USER"
validate_no_newline "监听地址" "$HOST"
validate_no_newline "监听端口" "$PORT"
[[ "$SERVICE_USER" != "root" ]] || fail "root 不能作为最终服务用户"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || fail "端口必须在 1 到 65535 之间"
[[ -f "$TEMPLATE" ]] || fail "找不到 systemd 模板: $TEMPLATE"
[[ -x "$APP_DIR/.venv/bin/gunicorn" ]] || fail "找不到 Gunicorn: $APP_DIR/.venv/bin/gunicorn"
[[ -f "$APP_DIR/.env" ]] || fail "找不到 .env；请先运行 bootstrap.sh"

SERVICE_RECORD="$(getent passwd "$SERVICE_USER" || true)"
[[ -n "$SERVICE_RECORD" ]] || fail "服务用户不存在: $SERVICE_USER"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_HOME="$(printf '%s\n' "$SERVICE_RECORD" | cut -d: -f6)"
[[ "$SERVICE_HOME" == /* ]] || fail "服务用户 HOME 必须是绝对路径"
validate_no_newline "服务组" "$SERVICE_GROUP"
validate_no_newline "服务用户 HOME" "$SERVICE_HOME"

escape_systemd_value() {
  systemd-escape -- "$1"
}

export SERVICE_USER_ESCAPED="$(escape_systemd_value "$SERVICE_USER")"
export SERVICE_GROUP_ESCAPED="$(escape_systemd_value "$SERVICE_GROUP")"
export SERVICE_HOME_ESCAPED="$(escape_systemd_value "$SERVICE_HOME")"
export APP_DIR_ESCAPED="$(escape_systemd_value "$APP_DIR")"
export ENV_FILE_ESCAPED="$(escape_systemd_value "$APP_DIR/.env")"
export GUNICORN_ESCAPED="$(escape_systemd_value "$APP_DIR/.venv/bin/gunicorn")"
export GUNICORN_CONFIG_ESCAPED="$(escape_systemd_value "$APP_DIR/deploy/linux/gunicorn.conf.py")"
export HOST_ESCAPED="$(escape_systemd_value "$HOST")"
export PORT_ESCAPED="$(escape_systemd_value "$PORT")"

TMP_UNIT="$(mktemp "/etc/systemd/system/.${SERVICE_NAME}.XXXXXX")"
cleanup() {
  rm -f "$TMP_UNIT"
}
trap cleanup EXIT

python3 - "$TEMPLATE" "$TMP_UNIT" <<'PY'
import os
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
replacements = {
    "__SERVICE_USER__": os.environ["SERVICE_USER_ESCAPED"],
    "__SERVICE_GROUP__": os.environ["SERVICE_GROUP_ESCAPED"],
    "__SERVICE_HOME__": os.environ["SERVICE_HOME_ESCAPED"],
    "__APP_DIR__": os.environ["APP_DIR_ESCAPED"],
    "__ENV_FILE__": os.environ["ENV_FILE_ESCAPED"],
    "__GUNICORN__": os.environ["GUNICORN_ESCAPED"],
    "__GUNICORN_CONFIG__": os.environ["GUNICORN_CONFIG_ESCAPED"],
    "__HOST__": os.environ["HOST_ESCAPED"],
    "__PORT__": os.environ["PORT_ESCAPED"],
}
for placeholder, value in replacements.items():
    template = template.replace(placeholder, value)
Path(sys.argv[2]).write_text(template, encoding="utf-8")
PY

chown root:root "$TMP_UNIT"
chmod 0644 "$TMP_UNIT"
mv -f "$TMP_UNIT" "$UNIT_PATH"
trap - EXIT

systemctl daemon-reload
if [[ "$START_SERVICE" -eq 1 ]]; then
  systemctl enable --now "${SERVICE_NAME}.service"
else
  systemctl enable "${SERVICE_NAME}.service"
fi

printf '已安装 systemd 服务: %s（用户: %s）\n' "$SERVICE_NAME" "$SERVICE_USER"
