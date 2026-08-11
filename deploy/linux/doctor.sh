#!/usr/bin/env bash
set -u -o pipefail

SERVICE_NAME="turb-gpt-register.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
HOST="127.0.0.1"
PORT="5000"
STATUS=0

usage() { printf '%s\n' 'Usage: deploy/linux/doctor.sh [--host HOST] [--port PORT]'; }
usage_error() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
validate_arguments() {
  [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || usage_error "port must be an integer from 1 to 65535"
  [[ "$HOST" != *$'\n'* && "$HOST" != *$'\r'* ]] || usage_error "host cannot contain newline characters"
  if [[ "$HOST" =~ ^\[(.*)\]$ ]]; then
    python3 - "$HOST" <<'PY' >/dev/null 2>&1 || usage_error "host must be a hostname, IPv4 address, or bracketed IPv6 address"
import ipaddress
import sys
ipaddress.IPv6Address(sys.argv[1][1:-1])
PY
    return
  fi
  [[ "$HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || usage_error "host must be a hostname, IPv4 address, or bracketed IPv6 address"
  [[ "$HOST" != *..* ]] || usage_error "host must be a hostname, IPv4 address, or bracketed IPv6 address"
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
    --host|--port)
      [[ $# -ge 2 && -n "$2" ]] || usage_error "$1 requires a value"
      if [[ "$1" == --host ]]; then HOST="$2"; else PORT="$2"; fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
done
validate_arguments

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|aarch64) printf '[OK] Architecture: %s\n' "$ARCH" ;;
  *) printf '[FAIL] Unsupported architecture: %s\n' "$ARCH" >&2; STATUS=1 ;;
esac

VENV_PYTHON="$APP_DIR/.venv/bin/python"
CLOAK_BINARY="$APP_DIR/.venv/bin/cloakbrowser"
check "virtual environment Python" test -x "$VENV_PYTHON"
check ".env exists" test -f "$APP_DIR/.env"
check "Cloak binary" test -x "$CLOAK_BINARY"

if systemctl is-active --quiet "$SERVICE_NAME"; then
  printf '[OK] systemd service is active\n'
else
  printf '[FAIL] systemd service is not active\n' >&2
  STATUS=1
fi
if curl --fail --silent --show-error --max-time 5 "http://${HOST}:${PORT}/login" -o /dev/null; then
  printf '[OK] Port and /login: http://%s:%s/login\n' "$HOST" "$PORT"
else
  printf '[FAIL] Port or /login unavailable: http://%s:%s/login\n' "$HOST" "$PORT" >&2
  STATUS=1
fi
CONTROL_GROUP="$(systemctl show --property=ControlGroup --value "$SERVICE_NAME" 2>/dev/null || true)"
MEMORY_FILE="/sys/fs/cgroup${CONTROL_GROUP}/memory.current"
if [[ -n "$CONTROL_GROUP" && -r "$MEMORY_FILE" ]]; then
  printf '[OK] cgroup memory: %s bytes\n' "$(<"$MEMORY_FILE")"
else
  printf '[FAIL] cannot read cgroup memory\n' >&2
  STATUS=1
fi
exit "$STATUS"
