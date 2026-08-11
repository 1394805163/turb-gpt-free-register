#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="turb-gpt-register"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
TEMPLATE="$SCRIPT_DIR/${SERVICE_NAME}.service.template"
UNIT_DIR="/etc/systemd/system"
UNIT_PATH="$UNIT_DIR/${SERVICE_NAME}.service"
SYSTEMCTL_BIN="/usr/bin/systemctl"
SYSTEMD_ANALYZE_BIN="/usr/bin/systemd-analyze"
SERVICE_USER=""
SERVICE_GROUP=""
SERVICE_HOME=""
CREATE_TURBGPT=0
HOST="127.0.0.1"
PORT="5000"
START_SERVICE=1
RENDER_ONLY=0
RENDER_OUTPUT=""
CHECK_ACCESS_ONLY=0
APPLY_UNIT_ONLY=0
TEST_ROOT=""
TEST_MODE=0
TMP_UNIT=""
BACKUP_UNIT=""

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
  --apply-unit-only
  --test-root DIR      Internal test seam; requires --apply-unit-only
EOF
}
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
require_value() { [[ $# -ge 2 && -n "$2" ]] || usage_error "$1 requires a value"; }
validate_no_newline() { [[ "$2" != *$'\n'* && "$2" != *$'\r'* ]] || usage_error "$1 cannot contain newline characters"; }

select_service_user() {
  local current_user
  if [[ -n "$SERVICE_USER" ]]; then return; fi
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then SERVICE_USER="$SUDO_USER"; return; fi
  current_user="$(id -un)"
  if [[ "$current_user" != root ]]; then SERVICE_USER="$current_user"; return; fi
  SERVICE_USER="turbgpt"
  CREATE_TURBGPT=1
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
ensure_service_user() {
  if [[ "$CREATE_TURBGPT" -eq 1 ]] && ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
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

# Keep directive paths and ExecStart argv literal; do not use unit-name escaping.
systemd_quote() {
  local value="$1"
  value="${value//%/%%}"
  value="${value//\\/\\\\\\\\}"
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
  for value in "$user" "$group" "$home_env" "$working_dir" "$env_file" "$host_env" "$port_env" "$gunicorn" "$config"; do
    [[ "$value" != *'@@TURB_RENDER_'* ]] || fail "rendered value collides with internal token sentinel"
  done
  : > "$output"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//__SERVICE_USER__/@@TURB_RENDER_USER@@}"
    line="${line//__SERVICE_GROUP__/@@TURB_RENDER_GROUP@@}"
    line="${line//__HOME_ENV__/@@TURB_RENDER_HOME@@}"
    line="${line//__APP_DIR__/@@TURB_RENDER_APP@@}"
    line="${line//__ENV_FILE__/@@TURB_RENDER_ENV_FILE@@}"
    line="${line//__HOST_ENV__/@@TURB_RENDER_HOST@@}"
    line="${line//__PORT_ENV__/@@TURB_RENDER_PORT@@}"
    line="${line//__GUNICORN_ARG__/@@TURB_RENDER_GUNICORN@@}"
    line="${line//__GUNICORN_CONFIG_ARG__/@@TURB_RENDER_CONFIG@@}"
    line="${line//@@TURB_RENDER_USER@@/$user}"
    line="${line//@@TURB_RENDER_GROUP@@/$group}"
    line="${line//@@TURB_RENDER_HOME@@/$home_env}"
    line="${line//@@TURB_RENDER_APP@@/$working_dir}"
    line="${line//@@TURB_RENDER_ENV_FILE@@/$env_file}"
    line="${line//@@TURB_RENDER_HOST@@/$host_env}"
    line="${line//@@TURB_RENDER_PORT@@/$port_env}"
    line="${line//@@TURB_RENDER_GUNICORN@@/$gunicorn}"
    line="${line//@@TURB_RENDER_CONFIG@@/$config}"
    printf '%s\n' "$line" >> "$output"
  done < "$TEMPLATE"
}
run_as_service_user() { runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"; }
check_service_user_access() {
  if ! run_as_service_user /bin/sh -c '
    app_dir="$1"
    cd -- "$app_dir" || exit 1
    test -r "$app_dir/requirements.txt" || exit 1
    test -r "$app_dir/web.py" || exit 1
    test -r "$app_dir/deploy/linux/gunicorn.conf.py" || exit 1
    test -x "$app_dir/.venv/bin/gunicorn" || exit 1
    test -r "$app_dir/.env" || exit 1
    test -w "$app_dir" || exit 1
    test -w "$app_dir/logs" || exit 1
    test -w "$app_dir/run" || exit 1
    test -w "$app_dir/data" || exit 1
  ' sh "$APP_DIR"; then
    fail "service user cannot traverse/read/write project and runtime directories"
  fi
}
prepare_service_access() {
  if [[ "$APP_DIR" == /opt/* ]]; then chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"; fi
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR/logs" "$APP_DIR/run" "$APP_DIR/data"
  [[ -f "$APP_DIR/.env" && ! -L "$APP_DIR/.env" ]] || fail ".env must be a regular file"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/.env"
  chmod 0600 "$APP_DIR/.env"
  check_service_user_access
}
validate_install_prerequisites() {
  [[ -f "$APP_DIR/requirements.txt" ]] || fail "requirements.txt not found"
  [[ -f "$APP_DIR/web.py" ]] || fail "web.py not found"
  [[ -f "$APP_DIR/deploy/linux/gunicorn.conf.py" ]] || fail "Gunicorn configuration not found"
  [[ -x "$APP_DIR/.venv/bin/gunicorn" ]] || fail "Gunicorn not found: $APP_DIR/.venv/bin/gunicorn"
  [[ -f "$APP_DIR/.env" ]] || fail ".env not found; run bootstrap.sh first"
}
configure_test_mode() {
  [[ "$(id -u)" -ne 0 ]] || usage_error "--test-root is only available to non-root tests"
  [[ "$TEST_ROOT" == /* ]] || usage_error "test root must be absolute"
  validate_no_newline "test root" "$TEST_ROOT"
  [[ "$TEST_ROOT" != /etc && "$TEST_ROOT" != /etc/* ]] || usage_error "test root cannot target /etc"
  UNIT_DIR="$TEST_ROOT/units"
  UNIT_PATH="$UNIT_DIR/${SERVICE_NAME}.service"
  SYSTEMCTL_BIN="$TEST_ROOT/systemctl"
  SYSTEMD_ANALYZE_BIN="$TEST_ROOT/systemd-analyze"
  [[ -d "$UNIT_DIR" && -x "$SYSTEMCTL_BIN" && -x "$SYSTEMD_ANALYZE_BIN" ]] || fail "test root is incomplete"
  TEST_MODE=1
}
cleanup() {
  [[ -n "$TMP_UNIT" ]] && rm -f "$TMP_UNIT"
  [[ -n "$BACKUP_UNIT" ]] && rm -f "$BACKUP_UNIT"
}
restore_previous_unit() {
  if [[ -n "$BACKUP_UNIT" ]]; then
    mv -f "$BACKUP_UNIT" "$UNIT_PATH"
    BACKUP_UNIT=""
  else
    rm -f "$UNIT_PATH"
  fi
}
apply_unit_transaction() {
  [[ -d "$UNIT_DIR" ]] || fail "unit directory does not exist: $UNIT_DIR"
  if [[ -f "$UNIT_PATH" ]]; then
    BACKUP_UNIT="$(mktemp "$UNIT_DIR/.${SERVICE_NAME}.backup.XXXXXX.service")"
    cp -p "$UNIT_PATH" "$BACKUP_UNIT"
  fi
  TMP_UNIT="$(mktemp "$UNIT_DIR/.${SERVICE_NAME}.new.XXXXXX.service")"
  render_unit "$TMP_UNIT"
  if [[ "$(id -u)" -eq 0 ]]; then chown root:root "$TMP_UNIT"; fi
  chmod 0644 "$TMP_UNIT"
  "$SYSTEMD_ANALYZE_BIN" verify "$TMP_UNIT" || fail "systemd-analyze verify failed; existing unit was not replaced"
  mv -f "$TMP_UNIT" "$UNIT_PATH"
  TMP_UNIT=""
  if ! "$SYSTEMCTL_BIN" daemon-reload; then
    restore_previous_unit
    "$SYSTEMCTL_BIN" daemon-reload || true
    fail "systemd daemon-reload failed; restored previous unit"
  fi
  if [[ "$APPLY_UNIT_ONLY" -eq 0 ]]; then
    if [[ "$START_SERVICE" -eq 1 ]]; then
      "$SYSTEMCTL_BIN" enable --now "${SERVICE_NAME}.service" || {
        restore_previous_unit; "$SYSTEMCTL_BIN" daemon-reload || true; fail "systemd enable failed; restored previous unit";
      }
    else
      "$SYSTEMCTL_BIN" enable "${SERVICE_NAME}.service" || {
        restore_previous_unit; "$SYSTEMCTL_BIN" daemon-reload || true; fail "systemd enable failed; restored previous unit";
      }
    fi
  fi
  [[ -n "$BACKUP_UNIT" ]] && rm -f "$BACKUP_UNIT"
  BACKUP_UNIT=""
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-user|--service-group|--service-home|--app-dir|--host|--port|--render-only|--test-root)
      require_value "$1" "${2:-}"
      case "$1" in
        --service-user) SERVICE_USER="$2" ;;
        --service-group) SERVICE_GROUP="$2" ;;
        --service-home) SERVICE_HOME="$2" ;;
        --app-dir) APP_DIR="$2" ;;
        --host) HOST="$2" ;;
        --port) PORT="$2" ;;
        --render-only) RENDER_ONLY=1; RENDER_OUTPUT="$2" ;;
        --test-root) TEST_ROOT="$2" ;;
      esac
      shift 2 ;;
    --no-start) START_SERVICE=0; shift ;;
    --check-access-only) CHECK_ACCESS_ONLY=1; shift ;;
    --apply-unit-only) APPLY_UNIT_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
done

[[ -f "$TEMPLATE" ]] || fail "systemd template not found: $TEMPLATE"
select_service_user
validate_inputs
if [[ "$RENDER_ONLY" -eq 1 ]]; then
  [[ -z "$TEST_ROOT" ]] || usage_error "--test-root is only valid with --apply-unit-only"
  [[ "$(id -u)" -ne 0 ]] || usage_error "--render-only is only available to non-root tests"
  resolve_service_identity
  validate_no_newline "render output path" "$RENDER_OUTPUT"
  [[ "$RENDER_OUTPUT" != /etc && "$RENDER_OUTPUT" != /etc/* ]] || usage_error "render output cannot target /etc"
  render_unit "$RENDER_OUTPUT"
  exit 0
fi
if [[ -n "$TEST_ROOT" ]]; then
  configure_test_mode
elif [[ "$APPLY_UNIT_ONLY" -eq 1 ]]; then
  usage_error "--apply-unit-only requires --test-root"
elif [[ "$(id -u)" -ne 0 ]]; then
  fail "root is required to install the systemd service"
fi
ensure_service_user
resolve_service_identity
if [[ "$CHECK_ACCESS_ONLY" -eq 1 ]]; then check_service_user_access; exit 0; fi
validate_install_prerequisites
if [[ "$TEST_MODE" -eq 0 ]]; then
  prepare_service_access
fi
trap cleanup EXIT
apply_unit_transaction
trap - EXIT
printf 'Installed systemd service: %s (user: %s)\n' "$SERVICE_NAME" "$SERVICE_USER"
