#!/usr/bin/env bash
set -u -o pipefail

SERVICE_NAME="turb-gpt-register.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
HOST="127.0.0.1"
PORT="5000"
SERVICE_USER=""
SERVICE_HOME="/var/lib/turb-gpt-register"
STATUS=0

usage() { printf '%s\n' 'Usage: deploy/linux/doctor.sh [--host HOST] [--port PORT] [--service-user USER] [--service-home HOME]'; }
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
  else
    validate_hostname "$HOST" || usage_error "host must be a hostname, IPv4 address, or bracketed IPv6 address"
  fi
  [[ "$SERVICE_HOME" == /* && "$SERVICE_HOME" != *$'\n'* && "$SERVICE_HOME" != *$'\r'* ]] || usage_error "service HOME must be an absolute path without newlines"
}
validate_hostname() {
  local host="$1" label
  [[ ${#host} -le 253 && "$host" != *..* ]] || return 1
  local IFS=.
  read -r -a labels <<< "$host"
  for label in "${labels[@]}"; do
    [[ -n "$label" && ${#label} -le 63 ]] || return 1
    [[ "$label" =~ ^[A-Za-z0-9]$ || "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])$ ]] || return 1
  done
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
    --host|--port|--service-user|--service-home)
      [[ $# -ge 2 && -n "$2" ]] || usage_error "$1 requires a value"
      case "$1" in
        --host) HOST="$2" ;;
        --port) PORT="$2" ;;
        --service-user) SERVICE_USER="$2" ;;
        --service-home) SERVICE_HOME="$2" ;;
      esac
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
check "virtual environment Python" test -x "$VENV_PYTHON"
check ".env exists" test -f "$APP_DIR/.env"

if [[ -z "$SERVICE_USER" ]]; then
  SERVICE_USER="$(systemctl show --property=User --value "$SERVICE_NAME" 2>/dev/null || true)"
  [[ -n "$SERVICE_USER" ]] || SERVICE_USER="turbgpt"
fi
service_identity_is_non_root() {
  local uid
  uid="$(id -u "$SERVICE_USER" 2>/dev/null)" || return 1
  [[ "$uid" =~ ^[0-9]+$ && "$uid" -ne 0 ]]
}
run_as_service_user() {
  local service_uid current_uid
  service_uid="$(id -u "$SERVICE_USER")" || return 1
  current_uid="$(id -u)"
  if [[ "$current_uid" == "$service_uid" ]]; then
    env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"
  elif [[ "$current_uid" -eq 0 ]]; then
    runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"
  else
    return 1
  fi
}
run_cloak_doctor() {
  [[ -x "$VENV_PYTHON" ]] || return 1
  run_as_service_user "$VENV_PYTHON" -m cloakbrowser doctor
}
check "service user exists and is non-root" service_identity_is_non_root
check "Cloak can launch its browser binary" run_cloak_doctor

if systemctl is-active --quiet "$SERVICE_NAME"; then
  printf '[OK] systemd service is active\n'
else
  printf '[FAIL] systemd service is not active\n' >&2
  STATUS=1
fi
HTTP_STATUS="$(curl --silent --show-error --max-time 5 --output /dev/null --write-out '%{http_code}' "http://${HOST}:${PORT}/login" 2>/dev/null || true)"
if [[ "$HTTP_STATUS" == "200" ]]; then
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
