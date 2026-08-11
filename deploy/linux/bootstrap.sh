#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
SERVICE_USER=""
SERVICE_GROUP=""
SERVICE_HOME="/var/lib/turb-gpt-register"
HOST="127.0.0.1"
PORT="5000"
NO_START=0
PRINT_SERVICE_USER=0
RESOLVE_SERVICE_USER_ONLY=0
IDENTITY_TEST_ROOT=""
ID_BIN="/usr/bin/id"
GETENT_BIN="/usr/bin/getent"
USERADD_BIN="/usr/sbin/useradd"

usage() {
  cat <<'EOF'
用法: sudo deploy/linux/bootstrap.sh [选项]

选项:
  --service-user USER  运行服务的普通用户
  --service-home HOME  服务状态 HOME（默认: /var/lib/turb-gpt-register）
  --host HOST          WebUI 监听地址（默认: 127.0.0.1）
  --port PORT          WebUI 监听端口（默认: 5000）
  --no-start           安装后不启动服务
  --print-service-user 输出选择的服务用户
  -h, --help           显示帮助
EOF
}
fail() { printf '错误: %s\n' "$*" >&2; exit 1; }
require_value() { [[ $# -ge 2 && -n "$2" ]] || fail "$1 需要一个值"; }

select_service_user() {
  local current_user
  if [[ -n "$SERVICE_USER" ]]; then
    return
  fi
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    SERVICE_USER="$SUDO_USER"
    return
  fi
  current_user="$("$ID_BIN" -un)"
  if [[ "$current_user" != "root" ]]; then
    SERVICE_USER="$current_user"
    return
  fi
  SERVICE_USER="turbgpt"
}

configure_identity_test_mode() {
  [[ -n "$IDENTITY_TEST_ROOT" ]] || return 0
  [[ "$RESOLVE_SERVICE_USER_ONLY" -eq 1 ]] || fail "--identity-test-root 只能与 --resolve-service-user-only 一起使用"
  [[ "$(/usr/bin/id -u)" -ne 0 ]] || fail "root 不能使用身份测试入口"
  [[ "$IDENTITY_TEST_ROOT" == /* && "$IDENTITY_TEST_ROOT" != *$'\n'* && "$IDENTITY_TEST_ROOT" != *$'\r'* ]] || fail "身份测试目录必须是无换行的绝对路径"
  ID_BIN="$IDENTITY_TEST_ROOT/id"
  GETENT_BIN="$IDENTITY_TEST_ROOT/getent"
  USERADD_BIN="$IDENTITY_TEST_ROOT/useradd"
  [[ -x "$ID_BIN" && -x "$GETENT_BIN" && -x "$USERADD_BIN" ]] || fail "身份测试目录缺少可执行夹具"
}

resolve_service_identity() {
  local record uid
  if ! record="$("$GETENT_BIN" passwd "$SERVICE_USER")"; then
    if [[ "$SERVICE_USER" != "turbgpt" ]]; then
      fail "服务用户不存在: $SERVICE_USER"
    fi
    "$USERADD_BIN" --system --create-home --home-dir "$SERVICE_HOME" --shell /usr/sbin/nologin "$SERVICE_USER"
    record="$("$GETENT_BIN" passwd "$SERVICE_USER")" || fail "创建服务用户后仍无法解析: $SERVICE_USER"
  fi
  uid="$("$ID_BIN" -u "$SERVICE_USER")" || fail "无法读取服务用户 UID: $SERVICE_USER"
  [[ "$uid" =~ ^[0-9]+$ ]] || fail "服务用户 UID 无效: $SERVICE_USER"
  [[ "$uid" -ne 0 ]] || fail "服务用户 UID 0 会以 root 运行，已拒绝: $SERVICE_USER"
  SERVICE_GROUP="$("$ID_BIN" -gn "$SERVICE_USER")" || fail "无法读取服务用户组: $SERVICE_USER"
  [[ -n "$record" && -n "$SERVICE_GROUP" ]] || fail "服务用户信息不完整: $SERVICE_USER"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-user|--service-home|--host|--port|--identity-test-root)
      require_value "$1" "${2:-}"
      case "$1" in
        --service-user) SERVICE_USER="$2" ;;
        --service-home) SERVICE_HOME="$2" ;;
        --host) HOST="$2" ;;
        --port) PORT="$2" ;;
        --identity-test-root) IDENTITY_TEST_ROOT="$2" ;;
      esac
      shift 2
      ;;
    --no-start) NO_START=1; shift ;;
    --print-service-user) PRINT_SERVICE_USER=1; shift ;;
    --resolve-service-user-only) RESOLVE_SERVICE_USER_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知选项: $1" ;;
  esac
done

configure_identity_test_mode
select_service_user
if [[ "$PRINT_SERVICE_USER" -eq 1 ]]; then
  printf '%s\n' "$SERVICE_USER"
  exit 0
fi

[[ "$("$ID_BIN" -u)" -eq 0 ]] || fail "请以 root 身份运行 bootstrap 脚本"
[[ "$APP_DIR" == /* ]] || fail "项目路径必须是绝对路径"
[[ "$APP_DIR" != *$'\n'* && "$APP_DIR" != *$'\r'* ]] || fail "项目路径不能包含换行符"
[[ "$SERVICE_USER" != root ]] || fail "root 不能作为最终服务用户"
[[ "$SERVICE_HOME" == /* ]] || fail "服务状态 HOME 必须是绝对路径"
[[ "$SERVICE_HOME" != *$'\n'* && "$SERVICE_HOME" != *$'\r'* ]] || fail "服务状态 HOME 不能包含换行符"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || fail "端口必须在 1 到 65535 之间"
resolve_service_identity
if [[ "$RESOLVE_SERVICE_USER_ONLY" -eq 1 ]]; then
  printf 'user=%s group=%s home=%s\n' "$SERVICE_USER" "$SERVICE_GROUP" "$SERVICE_HOME"
  exit 0
fi
[[ -f "$APP_DIR/requirements.txt" ]] || fail "找不到 requirements.txt"
[[ -f "$APP_DIR/.env.example" ]] || fail "找不到 .env.example"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
    printf '警告: 当前系统不是 Ubuntu 24.04（检测到: %s %s）；将继续执行，但请自行验证依赖兼容性。\n' "${ID:-unknown}" "${VERSION_ID:-unknown}" >&2
  fi
else
  printf '警告: 无法读取 /etc/os-release；将继续执行，但请自行验证系统兼容性。\n' >&2
fi
case "$(uname -m)" in
  x86_64) ;;
  aarch64) printf '提示: ARM64 使用 Cloak 免费 binary 前，请确认当前版本的兼容性。\n' >&2 ;;
  *) fail "仅支持 x86_64 和 aarch64，当前架构: $(uname -m)" ;;
esac

run_as_service_user() {
  runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" XDG_CACHE_HOME="$SERVICE_HOME/.cache" "$@"
}

prepare_private_directory() {
  local path="$1" mode owner_uid owner_gid expected_uid expected_gid
  [[ ! -L "$path" ]] || fail "私有运行目录不能是符号链接: $path"
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$path"
  mode="$(stat -c '%a' "$path")"
  owner_uid="$(stat -c '%u' "$path")"
  owner_gid="$(stat -c '%g' "$path")"
  expected_uid="$("$ID_BIN" -u "$SERVICE_USER")"
  expected_gid="$("$ID_BIN" -g "$SERVICE_USER")"
  [[ "$mode" == "700" && "$owner_uid" == "$expected_uid" && "$owner_gid" == "$expected_gid" ]] || fail "私有运行目录权限或所有者不正确: $path"
}

if [[ "$APP_DIR" == /opt/* ]]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
fi
prepare_private_directory "$SERVICE_HOME"
prepare_private_directory "$APP_DIR/logs"
prepare_private_directory "$APP_DIR/run"
prepare_private_directory "$APP_DIR/data"
if [[ ! -e "$APP_DIR/.env" ]]; then
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
[[ -f "$APP_DIR/.env" && ! -L "$APP_DIR/.env" ]] || fail ".env 必须是普通文件"
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
    test -w "$app_dir" || exit 1
    test -w "$app_dir/logs" || exit 1
    test -w "$app_dir/run" || exit 1
    test -w "$app_dir/data" || exit 1
  ' sh "$APP_DIR"; then
    fail "服务用户无法遍历或读取项目、配置和 .env，或无法写入项目根目录和运行目录"
  fi
}
check_source_and_runtime_access

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip curl ca-certificates
VENV_DIR="$APP_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then python3 -m venv "$VENV_DIR"; fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$VENV_DIR"
run_as_service_user "$VENV_PYTHON" -m pip install --upgrade pip
run_as_service_user "$VENV_PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
# install-deps 只安装 Chromium 所需的系统库，不下载 Playwright Chromium。
"$VENV_PYTHON" -m playwright install-deps chromium
run_as_service_user "$VENV_PYTHON" -m cloakbrowser install
run_as_service_user "$VENV_PYTHON" -m cloakbrowser doctor
run_as_service_user test -x "$APP_DIR/.venv/bin/gunicorn" || fail "服务用户无法执行 Gunicorn"
INSTALL_ARGS=(--service-user "$SERVICE_USER" --service-home "$SERVICE_HOME" --host "$HOST" --port "$PORT")
if [[ "$NO_START" -eq 1 ]]; then INSTALL_ARGS+=(--no-start); fi
"$SCRIPT_DIR/install-systemd.sh" "${INSTALL_ARGS[@]}"
printf 'Ubuntu 原生部署完成。\n'
