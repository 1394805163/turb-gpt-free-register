#!/usr/bin/env bash
set -euo pipefail

# Turb GPT Free Register WebUI 管理脚本
#
# 用法：
#   ./webui.sh start      启动 WebUI
#   ./webui.sh stop       关闭 WebUI
#   ./webui.sh restart    重启 WebUI
#   ./webui.sh status     查看状态
#   ./webui.sh logs       实时查看日志
#
# 可选环境变量：
#   HOST=127.0.0.1
#   PORT=5000
#   OPEN_BROWSER=1
#   VERBOSE=1
#   AUTH_CODE=xxx
#   EXTRA_ARGS="..."

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
PROC_ROOT="${PROC_ROOT:-/proc}"

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
  ./webui.sh start
  PORT=8000 OPEN_BROWSER=1 ./webui.sh start
  HOST=0.0.0.0 PORT=5000 ./webui.sh restart
EOF
}

is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
  [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null || true
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
  local proc_dir="$1" expected_gunicorn expected_config index arg gunicorn_index=-1
  local gunicorn_match=0 config_match=0 app_match=0
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
  gunicorn_match=1
  for ((index = gunicorn_index + 1; index < ${#PROC_ARGS[@]}; index++)); do
    arg="${PROC_ARGS[index]}"
    [[ "$arg" == "webui.app:create_app()" ]] && app_match=1
    if [[ "$arg" == "--config" && $((index + 1)) -lt ${#PROC_ARGS[@]} && "${PROC_ARGS[index + 1]}" == "$expected_config" ]]; then
      config_match=1
    elif [[ "$arg" == "--config=$expected_config" ]]; then
      config_match=1
    fi
  done
  [[ "$gunicorn_match" -eq 1 && "$config_match" -eq 1 && "$app_match" -eq 1 ]]
}

proc_cmdline_matches_legacy() {
  local proc_dir="$1" expected_entry index arg
  local entry_match=0 host_match=0 port_match=0
  expected_entry="$ROOT_DIR/web.py"
  read_proc_args "$proc_dir/cmdline" || return 1
  for ((index = 0; index < ${#PROC_ARGS[@]}; index++)); do
    arg="${PROC_ARGS[index]}"
    [[ "$arg" == "$expected_entry" ]] && entry_match=1
    if [[ "$arg" == "--host" && $((index + 1)) -lt ${#PROC_ARGS[@]} && "${PROC_ARGS[index + 1]}" == "$HOST" ]]; then
      host_match=1
    fi
    if [[ "$arg" == "--port" && $((index + 1)) -lt ${#PROC_ARGS[@]} && "${PROC_ARGS[index + 1]}" == "$PORT" ]]; then
      port_match=1
    fi
  done
  [[ "$entry_match" -eq 1 && "$host_match" -eq 1 && "$port_match" -eq 1 ]]
}

find_pids_by_port() {
  local proc_dir pid ppid parent_dir
  for proc_dir in "$PROC_ROOT"/[0-9]*; do
    [[ -d "$proc_dir" ]] || continue
    pid="${proc_dir##*/}"
    if proc_cmdline_matches_gunicorn "$proc_dir" && proc_environment_matches_endpoint "$proc_dir/environ"; then
      ppid="$(awk '$1 == "PPid:" { print $2; exit }' "$proc_dir/status" 2>/dev/null || true)"
      parent_dir="$PROC_ROOT/${ppid:-0}"
      if [[ -d "$parent_dir" ]] && proc_cmdline_matches_gunicorn "$parent_dir" && proc_environment_matches_endpoint "$parent_dir/environ"; then
        continue
      fi
      printf '%s\n' "$pid"
    elif proc_cmdline_matches_legacy "$proc_dir"; then
      printf '%s\n' "$pid"
    fi
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

collect_running_pids() {
  local pids=()
  local pid
  pid="$(read_pid)"
  if is_running "$pid"; then
    pids+=("$pid")
  fi

  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(find_pids_by_port)

  local unique=()
  local seen x
  for pid in "${pids[@]:-}"; do
    [[ -z "$pid" || "$pid" == "$$" ]] && continue
    seen=0
    for x in "${unique[@]:-}"; do
      [[ "$x" == "$pid" ]] && seen=1 && break
    done
    [[ "$seen" == "0" ]] && unique+=("$pid")
  done

  printf '%s\n' "${unique[@]:-}"
}

cmd_start() {
  local py pid
  local existing_pids=()
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && existing_pids+=("$pid")
  done < <(collect_running_pids)
  if [[ "${#existing_pids[@]}" -gt 0 ]]; then
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
  echo "$pid" > "$PID_FILE"

  sleep 1
  if is_running "$pid"; then
    echo "启动成功：PID=$pid"
  else
    echo "启动失败，请查看日志：$LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

cmd_stop() {
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(collect_running_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  echo "正在关闭 WebUI：PID=${pids[*]}"
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done

  local alive
  for _ in {1..15}; do
    alive=0
    for pid in "${pids[@]}"; do
      if is_running "$pid"; then
        alive=1
        break
      fi
    done
    [[ "$alive" == "0" ]] && break
    sleep 1
  done

  for pid in "${pids[@]}"; do
    if is_running "$pid"; then
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
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(collect_running_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    return 1
  fi

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
