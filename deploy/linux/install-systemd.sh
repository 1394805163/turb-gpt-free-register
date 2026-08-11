#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="turb-gpt-register"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
TEMPLATE="$SCRIPT_DIR/${SERVICE_NAME}.service.template"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="turbgpt"
SERVICE_GROUP=""
SERVICE_HOME=""
HOST="127.0.0.1"
PORT="5000"
START_SERVICE=1
RENDER_ONLY=0
RENDER_OUTPUT=""
CHECK_ACCESS_ONLY=0

usage() {
  cat <<'EOF'
Usage: sudo deploy/linux/install-systemd.sh [options]
  --service-user USER
  --service-group GROUP
  --service-home HOME
  --app-dir DIR
  --host HOST
  --port PORT
  --no-start
  --render-only FILE
  --check-access-only
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
require_value() { [[ $# -ge 2 && -n "$2" ]] || usage_error "$1 requires a value"; }
validate_no_newline() {
  [[ "$2" != *$'\n'* && "$2" != *$'\r'* ]] || usage_error "$1 cannot contain newline characters"
}
validate_inputs() {
  [[ "$APP_DIR" == /* ]] || usage_error "project path must be absolute"
  validate_no_newline "project path" "$APP_DIR"
  validate_no_newline "service user" "$SERVICE_USER"
  validate_no_newline "host" "$HOST"
  validate_no_newline "port" "$PORT"
  [[ "$SERVICE_USER" != root ]] || usage_error "root cannot be the service user"
  [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || usage_error "port must be an integer from 1 to 65535"
}
resolve_service_identity() {
  if [[ -z "$SERVICE_GROUP" || -z "$SERVICE_HOME" ]]; then
    local record
    record="$(getent passwd "$SERVICE_USER" || true)"
    [[ -n "$record" ]] || fail "service user does not exist: $SERVICE_USER"
    [[ -n "$SERVICE_GROUP" ]] || SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
    [[ -n "$SERVICE_HOME" ]] || SERVICE_HOME="$(printf '%s\n' "$record" | cut -d: -f6)"
  fi
  [[ "$SERVICE_HOME" == /* ]] || fail "service HOME must be absolute"
  validate_no_newline "service group" "$SERVICE_GROUP"
  validate_no_newline "service HOME" "$SERVICE_HOME"
}

# Quote systemd directive values and ExecStart argv without converting them to unit names.
systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\t'/\\t}"
  printf '"%s"' "$value"
}
render_unit() {
  local output="$1" line
  local user group home_env working_dir env_file host_env port_env gunicorn config
  user="$(systemd_quote "$SERVICE_USER")"
  group="$(systemd_quote "$SERVICE_GROUP")"
  home_env="$(systemd_quote "HOME=$SERVICE_HOME")"
  working_dir="$(systemd_quote "$APP_DIR")"
  env_file="$(systemd_quote "$APP_DIR/.env")"
  host_env="$(systemd_quote "HOST=$HOST")"
  port_env="$(systemd_quote "PORT=$PORT")"
  gunicorn="$(systemd_quote "$APP_DIR/.venv/bin/gunicorn")"
  config="$(systemd_quote "$APP_DIR/deploy/linux/gunicorn.conf.py")"
  : > "$output"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//__SERVICE_USER__/$user}"
    line="${line//__SERVICE_GROUP__/$group}"
    line="${line//__HOME_ENV__/$home_env}"
    line="${line//__APP_DIR__/$working_dir}"
    line="${line//__ENV_FILE__/$env_file}"
    line="${line//__HOST_ENV__/$host_env}"
    line="${line//__PORT_ENV__/$port_env}"
    line="${line//__GUNICORN_ARG__/$gunicorn}"
    line="${line//__GUNICORN_CONFIG_ARG__/$config}"
    printf '%s\n' "$line" >> "$output"
  done < "$TEMPLATE"
}
run_as_service_user() {
  runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"
}
check_service_user_access() {
  if ! run_as_service_user /bin/sh -c '
    app_dir="$1"
    cd -- "$app_dir" || exit 1
    test -r "$app_dir/requirements.txt" || exit 1
    test -r "$app_dir/web.py" || exit 1
    test -r "$app_dir/deploy/linux/gunicorn.conf.py" || exit 1
    test -x "$app_dir/.venv/bin/gunicorn" || exit 1
    test -r "$app_dir/.env" || exit 1
    test -w "$app_dir/logs" || exit 1
    test -w "$app_dir/run" || exit 1
    test -w "$app_dir/data" || exit 1
  ' sh "$APP_DIR"; then
    fail "service user cannot traverse/read project and configuration or write runtime directories"
  fi
}
prepare_service_access() {
  if [[ "$APP_DIR" == /opt/* ]]; then
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
  fi
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR/logs" "$APP_DIR/run" "$APP_DIR/data"
  [[ -f "$APP_DIR/.env" && ! -L "$APP_DIR/.env" ]] || fail ".env must be a regular file"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/.env"
  chmod 0600 "$APP_DIR/.env"
  check_service_user_access
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-user|--service-group|--service-home|--app-dir|--host|--port|--render-only)
      require_value "$1" "${2:-}"
      case "$1" in
        --service-user) SERVICE_USER="$2" ;;
        --service-group) SERVICE_GROUP="$2" ;;
        --service-home) SERVICE_HOME="$2" ;;
        --app-dir) APP_DIR="$2" ;;
        --host) HOST="$2" ;;
        --port) PORT="$2" ;;
        --render-only) RENDER_ONLY=1; RENDER_OUTPUT="$2" ;;
      esac
      shift 2
      ;;
    --no-start) START_SERVICE=0; shift ;;
    --check-access-only) CHECK_ACCESS_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
done

[[ -f "$TEMPLATE" ]] || fail "systemd template not found: $TEMPLATE"
validate_inputs
resolve_service_identity
if [[ "$RENDER_ONLY" -eq 1 ]]; then
  validate_no_newline "render output path" "$RENDER_OUTPUT"
  render_unit "$RENDER_OUTPUT"
  exit 0
fi
[[ "$EUID" -eq 0 ]] || fail "root is required to install the systemd service"
if [[ "$CHECK_ACCESS_ONLY" -eq 1 ]]; then
  check_service_user_access
  exit 0
fi
[[ -x "$APP_DIR/.venv/bin/gunicorn" ]] || fail "Gunicorn not found: $APP_DIR/.venv/bin/gunicorn"
[[ -f "$APP_DIR/.env" ]] || fail ".env not found; run bootstrap.sh first"
prepare_service_access

TMP_UNIT="$(mktemp "/etc/systemd/system/.${SERVICE_NAME}.XXXXXX")"
cleanup() { rm -f "$TMP_UNIT"; }
trap cleanup EXIT
render_unit "$TMP_UNIT"
chown root:root "$TMP_UNIT"
chmod 0644 "$TMP_UNIT"
mv -f "$TMP_UNIT" "$UNIT_PATH"
trap - EXIT
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$UNIT_PATH"
fi
systemctl daemon-reload
if [[ "$START_SERVICE" -eq 1 ]]; then
  systemctl enable --now "${SERVICE_NAME}.service"
else
  systemctl enable "${SERVICE_NAME}.service"
fi
printf 'Installed systemd service: %s (user: %s)\n' "$SERVICE_NAME" "$SERVICE_USER"
