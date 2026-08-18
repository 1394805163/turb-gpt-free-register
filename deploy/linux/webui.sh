#!/usr/bin/env bash
set -euo pipefail

# Turb GPT Free Register WebUI 管理脚本
#
# 用法：
#   ./deploy/linux/webui.sh start      启动 WebUI
#   ./deploy/linux/webui.sh stop       关闭 WebUI
#   ./deploy/linux/webui.sh restart    重启 WebUI
#   ./deploy/linux/webui.sh status     查看状态
#   ./deploy/linux/webui.sh logs       实时查看日志
#
# 可选环境变量：
#   HOST=127.0.0.1
#   PORT=5000
#   OPEN_BROWSER=1
#   VERBOSE=1
#   AUTH_CODE=xxx
#   EXTRA_ARGS="..."

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/run"
LOG_DIR="$ROOT_DIR/logs"
PID_FILE="$RUN_DIR/webui.pid"
LOG_FILE="$LOG_DIR/webui.log"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"
OPEN_BROWSER="${OPEN_BROWSER:-0}"
VERBOSE="${VERBOSE:-0}"
AUTH_CODE="${AUTH_CODE:-}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
PROC_ROOT="/proc"
if [[ "${WEBUI_TEST_MODE:-0}" == "1" ]]; then
  if [[ "$(/usr/bin/id -u)" -eq 0 ]]; then
    echo "WEBUI_TEST_MODE 不允许 root 使用" >&2
    exit 2
  fi
  if [[ -z "${WEBUI_TEST_PROC_ROOT:-}" || "$WEBUI_TEST_PROC_ROOT" != /* \
    || "$WEBUI_TEST_PROC_ROOT" == *$'\n'* || "$WEBUI_TEST_PROC_ROOT" == *$'\r'* ]]; then
    echo "WEBUI_TEST_PROC_ROOT 必须是无换行的绝对路径" >&2
    exit 2
  fi
  PROC_ROOT="$WEBUI_TEST_PROC_ROOT"
fi

mkdir -p "$RUN_DIR" "$LOG_DIR"

usage() {
  cat <<EOF
用法：$0 <command>

commands:
  start      启动 WebUI
  stop       关闭 WebUI
  restart    重启 WebUI
  status     查看运行状态
  logs       实时查看日志

环境变量：
  HOST=127.0.0.1 PORT=5000 OPEN_BROWSER=1 VERBOSE=1 AUTH_CODE=xxx EXTRA_ARGS="..."

示例：
  ./deploy/linux/webui.sh start
  PORT=8000 OPEN_BROWSER=1 ./deploy/linux/webui.sh start
  HOST=0.0.0.0 PORT=5000 ./deploy/linux/webui.sh restart
EOF
}

read_proc_args() {
  local cmdline="$1" arg
  PROC_ARGS=()
  [[ -r "$cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    PROC_ARGS+=("$arg")
  done < "$cmdline"
  [[ "${#PROC_ARGS[@]}" -gt 0 ]]
}

proc_environment_matches_endpoint() {
  local environ="$1" entry host_match=0 port_match=0
  [[ -r "$environ" ]] || return 1
  while IFS= read -r -d '' entry; do
    [[ "$entry" == "HOST=$HOST" ]] && host_match=1
    [[ "$entry" == "PORT=$PORT" ]] && port_match=1
  done < "$environ"
  [[ "$host_match" -eq 1 && "$port_match" -eq 1 ]]
}

proc_cmdline_matches_gunicorn() {
  local proc_dir="$1" expected_gunicorn expected_config gunicorn_index=-1 expected_count
  expected_gunicorn="$ROOT_DIR/.venv/bin/gunicorn"
  expected_config="$ROOT_DIR/deploy/linux/gunicorn.conf.py"
  read_proc_args "$proc_dir/cmdline" || return 1
  if [[ "${PROC_ARGS[0]}" == "$expected_gunicorn" ]]; then
    gunicorn_index=0
  elif [[ "${#PROC_ARGS[@]}" -ge 2 && "${PROC_ARGS[1]}" == "$expected_gunicorn" ]]; then
    case "${PROC_ARGS[0]}" in
      "$ROOT_DIR/.venv/bin/python"|"$ROOT_DIR/.venv/bin/python"[0-9]*) gunicorn_index=1 ;;
    esac
  fi
  [[ "$gunicorn_index" -ge 0 ]] || return 1
  expected_count=$((gunicorn_index + 4))
  [[ "${#PROC_ARGS[@]}" -eq "$expected_count" \
    && "${PROC_ARGS[gunicorn_index + 1]}" == "--config" \
    && "${PROC_ARGS[gunicorn_index + 2]}" == "$expected_config" \
    && "${PROC_ARGS[gunicorn_index + 3]}" == "webui.app:create_app()" ]]
}

proc_cmdline_matches_legacy() {
  local proc_dir="$1" expected_entry interpreter
  expected_entry="$ROOT_DIR/web.py"
  read_proc_args "$proc_dir/cmdline" || return 1
  [[ "${#PROC_ARGS[@]}" -ge 6 ]] || return 1
  interpreter="${PROC_ARGS[0]##*/}"
  [[ "$interpreter" == "python" || "$interpreter" == "python3" ]] || return 1
  [[ "${PROC_ARGS[1]}" == "$expected_entry" \
    && "${PROC_ARGS[2]}" == "--host" && "${PROC_ARGS[3]}" == "$HOST" \
    && "${PROC_ARGS[4]}" == "--port" && "${PROC_ARGS[5]}" == "$PORT" ]]
}

read_proc_starttime() {
  local pid="$1" stat_line remainder
  local fields=()
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "$PROC_ROOT/$pid/stat" ]] || return 1
  IFS= read -r stat_line < "$PROC_ROOT/$pid/stat" || return 1
  [[ "$stat_line" == *") "* ]] || return 1
  remainder="${stat_line##*) }"
  read -r -a fields <<< "$remainder"
  [[ "${#fields[@]}" -ge 20 && "${fields[19]}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${fields[19]}"
}

raw_gunicorn_identity_matches() {
  local pid="$1" proc_dir="$PROC_ROOT/$pid"
  proc_cmdline_matches_gunicorn "$proc_dir" \
    && proc_environment_matches_endpoint "$proc_dir/environ"
}

process_identity_matches() {
  local pid="$1" expected_starttime="${2:-}" current_starttime ppid
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  current_starttime="$(read_proc_starttime "$pid")" || return 1
  if [[ -n "$expected_starttime" && "$current_starttime" != "$expected_starttime" ]]; then
    return 1
  fi
  if raw_gunicorn_identity_matches "$pid"; then
    ppid="$(awk '$1 == "PPid:" { print $2; exit }' "$PROC_ROOT/$pid/status" 2>/dev/null || true)"
    if [[ "$ppid" =~ ^[1-9][0-9]*$ ]] && raw_gunicorn_identity_matches "$ppid"; then
      return 1
    fi
    return 0
  fi
  proc_cmdline_matches_legacy "$PROC_ROOT/$pid"
}

read_pid_record() {
  local pid starttime extra
  [[ -f "$PID_FILE" ]] || return 1
  IFS=' ' read -r pid starttime extra < "$PID_FILE" || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$starttime" =~ ^[0-9]+$ && -z "${extra:-}" ]] || return 1
  process_identity_matches "$pid" "$starttime" || return 1
  printf '%s %s\n' "$pid" "$starttime"
}

find_running_processes() {
  local proc_dir pid starttime
  for proc_dir in "$PROC_ROOT"/[0-9]*; do
    [[ -d "$proc_dir" ]] || continue
    pid="${proc_dir##*/}"
    starttime="$(read_proc_starttime "$pid")" || continue
    process_identity_matches "$pid" "$starttime" || continue
    printf '%s %s\n' "$pid" "$starttime"
  done
}

get_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    echo "$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "未找到 Python：请先创建 .venv 或安装 python3" >&2
    return 1
  fi
}

collect_running_processes() {
  local records=() unique=()
  local record pid starttime seen x
  record="$(read_pid_record 2>/dev/null || true)"
  [[ -z "$record" ]] || records+=("$record")
  while IFS=' ' read -r pid starttime; do
    [[ -n "$pid" && -n "$starttime" ]] && records+=("$pid $starttime")
  done < <(find_running_processes)
  for record in "${records[@]:-}"; do
    [[ -n "$record" ]] || continue
    pid="${record%% *}"
    seen=0
    for x in "${unique[@]:-}"; do
      [[ "${x%% *}" == "$pid" ]] && seen=1 && break
    done
    [[ "$seen" == "0" ]] && unique+=("$record")
  done
  printf '%s\n' "${unique[@]:-}"
}

cmd_start() {
  local py pid starttime record
  local existing_records=() existing_pids=()
  while IFS= read -r record; do
    [[ -n "$record" ]] && existing_records+=("$record")
  done < <(collect_running_processes)
  if [[ "${#existing_records[@]}" -gt 0 ]]; then
    for record in "${existing_records[@]}"; do existing_pids+=("${record%% *}"); done
    echo "WebUI 已在运行：PID=${existing_pids[*]}，地址：http://${HOST}:${PORT}"
    return 0
  fi
  rm -f "$PID_FILE"

  py="$(get_python)"

  local command=("$py" "$ROOT_DIR/web.py" "--host" "$HOST" "--port" "$PORT")
  if [[ -x "$ROOT_DIR/.venv/bin/gunicorn" ]]; then
    export HOST PORT
    command=("$ROOT_DIR/.venv/bin/gunicorn"
      --config "$ROOT_DIR/deploy/linux/gunicorn.conf.py"
      "webui.app:create_app()")
  else
    if [[ "$OPEN_BROWSER" == "1" || "$OPEN_BROWSER" == "true" ]]; then
      command+=("--open-browser")
    fi
    if [[ "$VERBOSE" == "1" || "$VERBOSE" == "true" ]]; then
      command+=("--verbose")
    fi
    if [[ -n "$AUTH_CODE" ]]; then
      command+=("--auth-code" "$AUTH_CODE")
    fi
    if [[ -n "$EXTRA_ARGS" ]]; then
      # shellcheck disable=SC2206
      local extra_parts=($EXTRA_ARGS)
      command+=("${extra_parts[@]}")
    fi
  fi

  echo "启动 WebUI：http://${HOST}:${PORT}"
  echo "日志文件：$LOG_FILE"
  nohup "${command[@]}" >> "$LOG_FILE" 2>&1 &
  pid=$!
  starttime="$(read_proc_starttime "$pid" 2>/dev/null || true)"
  if [[ -z "$starttime" ]]; then
    echo "启动失败，无法读取进程启动时间：PID=$pid" >&2
    kill "$pid" >/dev/null 2>&1 || true
    return 1
  fi
  printf '%s %s\n' "$pid" "$starttime" > "$PID_FILE"

  sleep 1
  if process_identity_matches "$pid" "$starttime"; then
    echo "启动成功：PID=$pid"
  else
    echo "启动失败，请查看日志：$LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

cmd_stop() {
  local records=() pids=()
  local record pid starttime
  while IFS= read -r record; do
    [[ -n "$record" ]] && records+=("$record")
  done < <(collect_running_processes)

  if [[ "${#records[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  for record in "${records[@]}"; do pids+=("${record%% *}"); done
  echo "正在关闭 WebUI：PID=${pids[*]}"
  for record in "${records[@]}"; do
    pid="${record%% *}"
    starttime="${record#* }"
    if process_identity_matches "$pid" "$starttime"; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done

  local alive
  for _ in {1..15}; do
    alive=0
    for record in "${records[@]}"; do
      pid="${record%% *}"
      starttime="${record#* }"
      if process_identity_matches "$pid" "$starttime"; then
        alive=1
        break
      fi
    done
    [[ "$alive" == "0" ]] && break
    sleep 1
  done

  for record in "${records[@]}"; do
    pid="${record%% *}"
    starttime="${record#* }"
    if process_identity_matches "$pid" "$starttime"; then
      echo "进程未退出，强制结束：PID=$pid"
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  done

  rm -f "$PID_FILE"
  echo "已关闭 WebUI"
}

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

cmd_status() {
  local records=() pids=()
  local record
  while IFS= read -r record; do
    [[ -n "$record" ]] && records+=("$record")
  done < <(collect_running_processes)

  if [[ "${#records[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    return 1
  fi

  for record in "${records[@]}"; do pids+=("${record%% *}"); done
  echo "WebUI 运行中：PID=${pids[*]}"
  echo "地址：http://${HOST}:${PORT}"
  echo "日志：$LOG_FILE"
}

cmd_logs() {
  touch "$LOG_FILE"
  tail -n 120 -f "$LOG_FILE"
}

cmd="${1:-}"
case "$cmd" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs|log) cmd_logs ;;
  -h|--help|help|"") usage ;;
  *)
    echo "未知命令：$cmd" >&2
    usage >&2
    exit 2
    ;;
esac
