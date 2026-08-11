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
??: sudo deploy/linux/bootstrap.sh [??]

??:
  --service-user USER  ????????????: turbgpt?
  --host HOST          WebUI ???????: 127.0.0.1?
  --port PORT          WebUI ???????: 5000?
  --no-start           ????????
  -h, --help           ????
EOF
}

fail() {
  printf '??: %s\n' "$*" >&2
  exit 1
}

require_value() {
  [[ $# -ge 2 && -n "$2" ]] || fail "$1 ?????"
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
      fail "????: $1"
      ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "?? root ???? bootstrap ??"
[[ "$APP_DIR" == /* ]] || fail "???????????"
[[ "$APP_DIR" != *$'\n'* && "$APP_DIR" != *$'\r'* ]] || fail "???????????"
[[ "$SERVICE_USER" != "root" ]] || fail "root ??????????"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || fail "????? 1 ? 65535 ??"
[[ -f "$APP_DIR/requirements.txt" ]] || fail "??? requirements.txt"
[[ -f "$APP_DIR/.env.example" ]] || fail "??? .env.example"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    printf '??: ?????? Ubuntu 24.04????: %s %s????????????????????\n' \
      "${ID:-unknown}" "${VERSION_ID:-unknown}" >&2
  fi
else
  printf '??: ???? /etc/os-release???????????????????\n' >&2
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)
    ;;
  aarch64)
    printf '??: ARM64 ?? Cloak ?? binary ??????????????\n' >&2
    ;;
  *)
    fail "??? x86_64 ? aarch64?????: $ARCH"
    ;;
esac

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[[ "$SERVICE_HOME" == /* ]] || fail "???? HOME ???????"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SERVICE_HOME"

run_as_service_user() {
  runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"
}

# /opt ????????????????????????????????????
if [[ "$APP_DIR" == /opt/* ]]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
fi

install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
  "$APP_DIR/logs" "$APP_DIR/run" "$APP_DIR/data"

if [[ ! -e "$APP_DIR/.env" ]]; then
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
    "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
[[ -f "$APP_DIR/.env" && ! -L "$APP_DIR/.env" ]] || fail ".env ???????"
# ???????????? .env ????????????????????
chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/.env"
chmod 0600 "$APP_DIR/.env"

check_source_and_runtime_access() {
  if ! run_as_service_user /bin/sh -c '
    app_dir="$1"
    cd -- "$app_dir" || exit 1
    test -r "$app_dir/requirements.txt" || exit 1
    test -r "$app_dir/web.py" || exit 1
    test -r "$app_dir/deploy/linux/gunicorn.conf.py" || exit 1
    test -r "$app_dir/.env" || exit 1
    test -w "$app_dir/logs" || exit 1
    test -w "$app_dir/run" || exit 1
    test -w "$app_dir/data" || exit 1
  ' sh "$APP_DIR"; then
    fail "????????????????? .env??????????"
  fi
}

check_source_and_runtime_access

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip curl ca-certificates

VENV_DIR="$APP_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  python3 -m venv "$VENV_DIR"
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$VENV_DIR"

run_as_service_user "$VENV_PYTHON" -m pip install --upgrade pip
run_as_service_user "$VENV_PYTHON" -m pip install -r "$APP_DIR/requirements.txt"

# install-deps ??? Chromium ?????????? Playwright Chromium?
"$VENV_PYTHON" -m playwright install-deps chromium

run_as_service_user "$VENV_PYTHON" -m cloakbrowser install
run_as_service_user "$VENV_PYTHON" -m cloakbrowser doctor --quick

if ! run_as_service_user test -x "$APP_DIR/.venv/bin/gunicorn"; then
  fail "???????? Gunicorn"
fi

INSTALL_ARGS=(--service-user "$SERVICE_USER" --host "$HOST" --port "$PORT")
if [[ "$NO_START" -eq 1 ]]; then
  INSTALL_ARGS+=(--no-start)
fi
"$SCRIPT_DIR/install-systemd.sh" "${INSTALL_ARGS[@]}"

printf 'Ubuntu ???????\n'
