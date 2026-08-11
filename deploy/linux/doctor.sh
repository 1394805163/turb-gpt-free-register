#!/usr/bin/env bash
set -u -o pipefail

SERVICE_NAME="turb-gpt-register.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
HOST="127.0.0.1"
PORT="5000"
STATUS=0

usage() {
  cat <<'EOF'
用法: deploy/linux/doctor.sh [--host HOST] [--port PORT]
EOF
}

check() {
  local label="$1"
  shift
  if "$@"; then
    printf '[OK] %s\n' "$label"
  else
    printf '[FAIL] %s\n' "$label" >&2
    STATUS=1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 && -n "$2" ]] || { printf '错误: --host 需要一个值\n' >&2; exit 2; }
      HOST="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 && -n "$2" ]] || { printf '错误: --port 需要一个值\n' >&2; exit 2; }
      PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '错误: 未知选项: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|aarch64)
    printf '[OK] 架构: %s\n' "$ARCH"
    ;;
  *)
    printf '[FAIL] 不支持的架构: %s\n' "$ARCH" >&2
    STATUS=1
    ;;
esac

VENV_PYTHON="$APP_DIR/.venv/bin/python"
CLOAK_BINARY="$APP_DIR/.venv/bin/cloakbrowser"
check "虚拟环境 Python" test -x "$VENV_PYTHON"
check ".env 文件存在" test -f "$APP_DIR/.env"
check "Cloak binary" test -x "$CLOAK_BINARY"

if systemctl is-active --quiet "$SERVICE_NAME"; then
  printf '[OK] systemd 服务处于 active 状态\n'
else
  printf '[FAIL] systemd 服务未处于 active 状态\n' >&2
  STATUS=1
fi

if curl --fail --silent --show-error --max-time 5 \
  "http://${HOST}:${PORT}/login" -o /dev/null; then
  printf '[OK] 端口和 /login: http://%s:%s/login\n' "$HOST" "$PORT"
else
  printf '[FAIL] 端口或 /login 不可用: http://%s:%s/login\n' "$HOST" "$PORT" >&2
  STATUS=1
fi

CONTROL_GROUP="$(systemctl show --property=ControlGroup --value "$SERVICE_NAME" 2>/dev/null || true)"
MEMORY_FILE="/sys/fs/cgroup${CONTROL_GROUP}/memory.current"
if [[ -n "$CONTROL_GROUP" && -r "$MEMORY_FILE" ]]; then
  MEMORY_BYTES="$(<"$MEMORY_FILE")"
  printf '[OK] cgroup 内存: %s bytes\n' "$MEMORY_BYTES"
else
  printf '[FAIL] 无法读取 cgroup 内存\n' >&2
  STATUS=1
fi

exit "$STATUS"
