#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# Quiet mode: prevent log pollution for parsable outputs
# -----------------------------
QUIET=0
if [[ "${1:-}" == "--current-raw" || "${1:-}" == "--current" || "${1:-}" == "--list" || "${1:-}" == "--list-raw" ]]; then
  QUIET=1
fi

SERVICE_GLOB="backhaul-iran*.service"

# primary failover anti-flap (per primary)
FAIL_THRESHOLD=3
STATE_FILE="/run/backhaul-failover.state"

# fatal log detection
FATAL_WINDOW="10 minutes ago"
FATAL_REGEX="(panic|fatal|segfault|address already in use|bind failed|cannot bind|permission denied)"

# candidate probing
CANDIDATE_WAIT_SEC=30
CANDIDATE_POLL_SEC=3

# ---- Traffic health (10-min drop detection) ----
# Compare avg traffic in last 10m vs previous 10m. If drop >= 90% -> unhealthy.
TRAFFIC_DROP_WINDOW_SEC=600     # 10 minutes
TRAFFIC_DROP_THRESHOLD_PCT=90   # drop >= 90% => bad
TRAFFIC_LOG_DIR="/run/backhaul-traffic"

# Fallback (optional): if prev window too small, ignore drop check
TRAFFIC_MIN_PREV_BPS=1024       # 1KB/s

LOCKFILE="/run/backhaul-failover.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  [[ "$QUIET" == "1" ]] || echo "[$(date '+%F %T')] 🟡 Lock busy; skipping this run."
  exit 0
fi
[[ "$QUIET" == "1" ]] || echo "[$(date '+%F %T')] ℹ️  Lock acquired."

USE_COLOR=1
if [[ ! -t 1 ]]; then USE_COLOR=0; fi

c_red(){   [[ "$USE_COLOR" == "1" ]] && printf '\033[31m%s\033[0m\n' "$*" || echo "$*"; }
c_green(){ [[ "$USE_COLOR" == "1" ]] && printf '\033[32m%s\033[0m\n' "$*" || echo "$*"; }
c_yel(){   [[ "$USE_COLOR" == "1" ]] && printf '\033[33m%s\033[0m\n' "$*" || echo "$*"; }
c_cyan(){  [[ "$USE_COLOR" == "1" ]] && printf '\033[36m%s\033[0m\n' "$*" || echo "$*"; }

log_info(){   echo "[$(date '+%F %T')] ℹ️  $*"; }
log_ok(){     c_green "[$(date '+%F %T')] ✅ $*"; }
log_warn(){   c_yel   "[$(date '+%F %T')] 🟡 $*"; }
log_bad(){    c_red   "[$(date '+%F %T')] ❌ $*"; }
log_switch(){ c_cyan  "[$(date '+%F %T')] 🔁 $*"; }

list_services() {
  systemctl list-units --type=service --all --no-legend --no-pager 2>/dev/null \
    | awk '{print $1}' \
    | grep -E "^backhaul-iran[0-9]+\.service$"
}

svc_num() { echo "$1" | grep -Eo '[0-9]+' | tail -n1; }

sort_by_priority() {
  while read -r s; do
    [[ -z "${s:-}" ]] && continue
    local n
    n="$(svc_num "$s" || true)"
    [[ -n "${n:-}" ]] || n=999999
    echo "$n $s"
  done | sort -n | awk '{print $2}'
}

is_active() { systemctl is-active --quiet "$1" 2>/dev/null; }
get_execstart() { systemctl show "$1" -p ExecStart --value --no-pager 2>/dev/null || true; }

get_toml_from_unit() {
  local svc="$1" ex
  ex="$(get_execstart "$svc")"
  echo "$ex" | grep -Eo '/[^ ;"]+\.toml' | head -n1 || true
}

get_bind_port_from_toml() {
  local toml="$1"
  [[ -f "$toml" ]] || return 1
  local line port
  line="$(grep -E '^[[:space:]]*bind_addr[[:space:]]*=' "$toml" | head -n1 || true)"
  port="$(echo "$line" | grep -Eo '":[0-9]+"' | grep -Eo '[0-9]+' | head -n1 || true)"
  [[ -n "${port:-}" ]] || return 1
  echo "$port"
}

is_listening() {
  local port="$1"
  ss -lnt 2>/dev/null | grep -Eq "LISTEN.*:${port}\b"
}

has_fatal_errors_recently() {
  local svc="$1"
  (journalctl -u "$svc" --since "$FATAL_WINDOW" --no-pager 2>/dev/null || true) \
    | grep -Eiq "$FATAL_REGEX" && return 0
  return 1
}

has_established_now() {
  local port="$1"
  ss -nt state established 2>/dev/null | grep -Eq "[:.]${port}\b"
}

# ---- Traffic helpers (TCP only) ----
get_port_bytes_sum_now() {
  local port="$1"
  ss -tinH "( sport = :$port )" 2>/dev/null \
    | awk '{
        for (i=1;i<=NF;i++){
          if ($i ~ /^bytes_received:/){ sub("bytes_received:","",$i); rx += $i }
          else if ($i ~ /^bytes_acked:/){ sub("bytes_acked:","",$i); tx += $i }
        }
      }
      END { printf "%.0f\n", (rx+tx) }'
}

ensure_traffic_dir() { mkdir -p "$TRAFFIC_LOG_DIR" 2>/dev/null || true; }
traffic_log_file() { local port="$1"; echo "${TRAFFIC_LOG_DIR}/port-${port}.log"; }

traffic_record_sample() {
  local port="$1"
  ensure_traffic_dir
  local f ts b
  f="$(traffic_log_file "$port")"
  ts="$(date +%s)"
  b="$(get_port_bytes_sum_now "$port" 2>/dev/null || echo 0)"
  echo "$ts $b" >> "$f"
  tail -n 200 "$f" > "${f}.tmp" 2>/dev/null && mv -f "${f}.tmp" "$f" 2>/dev/null || true
}

traffic_sample_at_or_before() {
  local f="$1" target="$2"
  awk -v t="$target" '
    ($1 <= t) { last_ts=$1; last_b=$2 }
    END { if (last_ts=="") exit 1; print last_ts, last_b }
  ' "$f"
}

traffic_avg_bps_between() {
  local port="$1" start="$2" end="$3"
  local f s e
  f="$(traffic_log_file "$port")"
  [[ -f "$f" ]] || { echo 0; return 0; }

  s="$(traffic_sample_at_or_before "$f" "$start" 2>/dev/null || true)"
  e="$(traffic_sample_at_or_before "$f" "$end" 2>/dev/null || true)"
  [[ -n "$s" && -n "$e" ]] || { echo 0; return 0; }

  local ts1 b1 ts2 b2 dt db
  ts1="$(awk '{print $1}' <<<"$s")"; b1="$(awk '{print $2}' <<<"$s")"
  ts2="$(awk '{print $1}' <<<"$e")"; b2="$(awk '{print $2}' <<<"$e")"

  dt=$((ts2-ts1))
  db=$((b2-b1))
  if (( dt <= 0 || db < 0 )); then
    echo 0; return 0
  fi
  echo $(( db / dt ))
}

traffic_drop_stats() {
  local port="$1"
  traffic_record_sample "$port"

  local now cur_start cur_end prev_start prev_end
  now="$(date +%s)"
  cur_end="$now"
  cur_start=$((now - TRAFFIC_DROP_WINDOW_SEC))
  prev_end="$cur_start"
  prev_start=$((prev_end - TRAFFIC_DROP_WINDOW_SEC))

  local prev_bps cur_bps
  prev_bps="$(traffic_avg_bps_between "$port" "$prev_start" "$prev_end")"
  cur_bps="$(traffic_avg_bps_between "$port" "$cur_start" "$cur_end")"

  local drop=0
  if (( prev_bps > 0 )); then
    drop=$(( 100 - (cur_bps * 100 / prev_bps) ))
    if (( drop < 0 )); then drop=0; fi
    if (( drop > 100 )); then drop=100; fi
  fi

  echo "$prev_bps $cur_bps $drop"
}

has_traffic_drop_10m() {
  local port="$1"
  local prev_bps cur_bps drop
  read -r prev_bps cur_bps drop < <(traffic_drop_stats "$port")
  if (( prev_bps < TRAFFIC_MIN_PREV_BPS )); then
    return 1
  fi
  (( drop >= TRAFFIC_DROP_THRESHOLD_PCT ))
}

is_healthy() {
  local svc="$1" bind_port="$2"
  is_active "$svc" || return 1
  is_listening "$bind_port" || return 1
  has_fatal_errors_recently "$svc" && return 1
  has_established_now "$bind_port" || return 1
  has_traffic_drop_10m "$bind_port" && return 1
  return 0
}

state_read(){ [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" 2>/dev/null || true; }
state_write(){ echo "$1 $2" > "$STATE_FILE"; }
state_reset(){ rm -f "$STATE_FILE" 2>/dev/null || true; }

get_primary_from_actives() {
  local actives=()
  while read -r s; do
    [[ -z "${s:-}" ]] && continue
    if is_active "$s"; then actives+=("$s"); fi
  done < <(list_services | sort_by_priority)

  if [[ "${#actives[@]}" -eq 0 ]]; then
    echo ""
    return 1
  fi

  printf "%s\n" "${actives[@]}" | sort_by_priority | head -n1
}

wait_until_healthy() {
  local svc="$1" port="$2"
  local waited=0
  while [[ "$waited" -lt "$CANDIDATE_WAIT_SEC" ]]; do
    if is_healthy "$svc" "$port"; then
      return 0
    fi
    sleep "$CANDIDATE_POLL_SEC"
    waited=$((waited + CANDIDATE_POLL_SEC))
  done
  return 1
}

stop_and_wait_inactive() {
  local svc="$1"
  systemctl stop "$svc" >/dev/null 2>&1 || true

  local i
  for i in {1..10}; do
    systemctl is-active --quiet "$svc" || return 0
    sleep 1
  done

  systemctl kill "$svc" >/dev/null 2>&1 || true
  systemctl stop "$svc" >/dev/null 2>&1 || true
  systemctl reset-failed "$svc" >/dev/null 2>&1 || true
  return 0
}

# ---- CLI helpers ----
cmd_list_raw() {
  mapfile -t svcs_all < <(list_services | sort_by_priority)
  for s in "${svcs_all[@]}"; do
    local toml port act
    toml="$(get_toml_from_unit "$s")"
    port="$(get_bind_port_from_toml "$toml" 2>/dev/null || echo "-")"
    if is_active "$s"; then act="yes"; else act="no"; fi
    printf "%s\t%s\t%s\t%s\n" "$s" "$act" "$port" "${toml:-"-"}"
  done
}

cmd_list() {
  mapfile -t svcs_all < <(list_services | sort_by_priority)
  if [[ "${#svcs_all[@]}" -eq 0 ]]; then
    echo "No services match: $SERVICE_GLOB"
    exit 0
  fi

  printf "%-35s %-8s %-7s %s\n" "SERVICE" "ACTIVE" "PORT" "TOML"
  for s in "${svcs_all[@]}"; do
    local toml port act
    toml="$(get_toml_from_unit "$s")"
    port="$(get_bind_port_from_toml "$toml" 2>/dev/null || echo "-")"
    if is_active "$s"; then act="yes"; else act="no"; fi
    printf "%-35s %-8s %-7s %s\n" "$s" "$act" "$port" "${toml:-"-"}"
  done
}

cmd_current() {
  local primary ptoml pport
  primary="$(get_primary_from_actives || true)"
  [[ -n "${primary:-}" ]] || { echo ""; exit 1; }

  ptoml="$(get_toml_from_unit "$primary")"
  pport="$(get_bind_port_from_toml "$ptoml" || true)"
  [[ -n "${pport:-}" ]] || { echo ""; exit 1; }

  echo "$primary $pport"
}

cmd_current_raw() {
  local primary ptoml pport
  primary="$(get_primary_from_actives || true)"
  [[ -n "${primary:-}" ]] || exit 1

  ptoml="$(get_toml_from_unit "$primary")"
  pport="$(get_bind_port_from_toml "$ptoml" || true)"
  [[ -n "${pport:-}" ]] || exit 1

  # MUST be exactly one clean line, tab-separated
  printf "%s\t%s\n" "$primary" "$pport"
}

cmd_watch_traffic() {
  local port="${1:-}"
  local interval="${2:-1}"
  [[ -n "${port:-}" ]] || { echo "Usage: $0 --watch-traffic <port> [interval]"; exit 2; }

  # FIX: validate numeric
  [[ "$port" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid port: '$port'"; exit 2; }

  local prev_b prev_t cur_b cur_t dt db bps kb mb
  prev_b="$(get_port_bytes_sum_now "$port" || echo 0)"
  prev_t="$(date +%s)"

  echo "Watching traffic on TCP sport=:${port}  (Ctrl+C to exit)"
  while true; do
    sleep "$interval"
    cur_b="$(get_port_bytes_sum_now "$port" || echo 0)"
    cur_t="$(date +%s)"
    dt=$((cur_t - prev_t))
    db=$((cur_b - prev_b))

    if (( dt > 0 && db >= 0 )); then
      bps=$((db/dt))
      kb=$((bps/1024))
      mb=$((bps/1024/1024))
      printf ":%s  %10s B/s  (%7s KB/s)  (%5s MB/s)\n" "$port" "$bps" "$kb" "$mb"
    else
      printf ":%s  N/A\n" "$port"
    fi

    prev_b=$cur_b
    prev_t=$cur_t
  done
}

cmd_start()   { systemctl start   "$1" || true; }
cmd_stop()    { systemctl stop    "$1" || true; }
cmd_restart() { systemctl restart "$1" || true; }

cmd_switch() {
  local target="$1"
  [[ -n "${target:-}" ]] || { echo "Usage: $0 --switch <service>"; exit 2; }

  mapfile -t svcs_all < <(list_services | sort_by_priority)
  local found=0
  for s in "${svcs_all[@]}"; do
    [[ "$s" == "$target" ]] && found=1
  done
  (( found == 1 )) || { echo "Service not found: $target"; exit 1; }

  local ttoml tport
  ttoml="$(get_toml_from_unit "$target")"
  tport="$(get_bind_port_from_toml "$ttoml" || true)"
  [[ -n "${tport:-}" ]] || { echo "Cannot parse bind port for $target (toml=$ttoml)"; exit 1; }

  local current
  current="$(get_primary_from_actives || true)"

  echo "[*] Manual switch to: $target (:$tport)"
  systemctl start "$target" >/dev/null 2>&1 || true

  if wait_until_healthy "$target" "$tport"; then
    echo "[+] Target is healthy. Enforcing single-active policy."
    if [[ -n "${current:-}" && "$current" != "$target" ]]; then
      stop_and_wait_inactive "$current"
    fi
    for s in "${svcs_all[@]}"; do
      [[ "$s" == "$target" ]] && continue
      if is_active "$s"; then stop_and_wait_inactive "$s"; fi
    done
    state_reset
    exit 0
  else
    echo "[-] Target did not become healthy; stopping it."
    stop_and_wait_inactive "$target"
    exit 1
  fi
}

main() {
  mapfile -t svcs_all < <(list_services | sort_by_priority)
  if [[ "${#svcs_all[@]}" -lt 2 ]]; then
    log_warn "Need at least 2 services matching ${SERVICE_GLOB}"
    exit 0
  fi

  local primary
  primary="$(get_primary_from_actives || true)"
  if [[ -z "${primary:-}" ]]; then
    log_warn "No active backhaul service found. Doing nothing."
    exit 0
  fi

  local ptoml pport
  ptoml="$(get_toml_from_unit "$primary")"
  pport="$(get_bind_port_from_toml "$ptoml" || true)"
  if [[ -z "${pport:-}" ]]; then
    log_bad "Could not parse primary bind port: $primary (toml=$ptoml)"
    exit 0
  fi

  local backups=()
  for s in "${svcs_all[@]}"; do
    [[ "$s" == "$primary" ]] && continue
    backups+=("$s")
  done

  local prev_bps cur_bps drop
  read -r prev_bps cur_bps drop < <(traffic_drop_stats "$pport")
  log_info "Primary=$primary :$pport | Backups=${#backups[@]} | Threshold=${FAIL_THRESHOLD} | DropCheck=10m drop>=${TRAFFIC_DROP_THRESHOLD_PCT}% | prev10m=$((prev_bps/1024))KB/s cur10m=$((cur_bps/1024))KB/s drop=${drop}%"

  if is_healthy "$primary" "$pport"; then
    log_ok "Primary healthy: $primary (:$pport)"
    state_reset

    for b in "${backups[@]}"; do
      if is_active "$b"; then
        log_warn "Stopping backup (policy): $b"
        stop_and_wait_inactive "$b"
        log_ok "Backup stopped: $b"
      fi
    done
    exit 0
  fi

  local last_svc last_cnt
  last_svc="$(state_read | awk '{print $1}' 2>/dev/null || true)"
  last_cnt="$(state_read | awk '{print $2}' 2>/dev/null || true)"
  [[ -n "${last_cnt:-}" ]] || last_cnt=0
  if [[ "$last_svc" != "$primary" ]]; then last_cnt=0; fi
  last_cnt=$((last_cnt + 1))
  state_write "$primary" "$last_cnt"

  log_bad "Primary unhealthy: $primary (:$pport) [count=${last_cnt}/${FAIL_THRESHOLD}] prev10m=$((prev_bps/1024))KB/s cur10m=$((cur_bps/1024))KB/s drop=${drop}%"

  if [[ "$last_cnt" -lt "$FAIL_THRESHOLD" ]]; then
    log_warn "Failover suppressed (anti-flap). Waiting for next check."
    exit 0
  fi

  local chosen=""
  for b in "${backups[@]}"; do
    local btoml bport
    btoml="$(get_toml_from_unit "$b")"
    bport="$(get_bind_port_from_toml "$btoml" || true)"
    if [[ -z "${bport:-}" ]]; then
      log_warn "Skipping candidate (no bind port): $b"
      continue
    fi

    log_switch "Trying candidate: $b (:$bport)"
    systemctl start "$b" >/dev/null 2>&1 || true

    if wait_until_healthy "$b" "$bport"; then
      chosen="$b"
      log_ok "Candidate healthy: $b (:$bport)"
      break
    fi

    log_warn "Candidate not healthy, stopping it: $b"
    stop_and_wait_inactive "$b"
  done

  if [[ -z "${chosen:-}" ]]; then
    log_bad "No healthy backup found. Keeping primary running."
    exit 0
  fi

  log_switch "Switching: stopping primary: $primary"
  stop_and_wait_inactive "$primary"
  log_bad "Primary stopped: $primary"

  for b in "${backups[@]}"; do
    [[ "$b" == "$chosen" ]] && continue
    if is_active "$b"; then
      log_warn "Stopping non-chosen backup: $b"
      stop_and_wait_inactive "$b"
    fi
  done

  state_reset
}

# ---- dispatcher ----
if [[ "${1:-}" == "--list" ]]; then
  cmd_list; exit 0
elif [[ "${1:-}" == "--list-raw" ]]; then
  cmd_list_raw; exit 0
elif [[ "${1:-}" == "--current" ]]; then
  cmd_current; exit 0
elif [[ "${1:-}" == "--current-raw" ]]; then
  cmd_current_raw; exit 0
elif [[ "${1:-}" == "--watch-traffic" ]]; then
  cmd_watch_traffic "${2:-}" "${3:-1}"; exit 0
elif [[ "${1:-}" == "--switch" ]]; then
  cmd_switch "${2:-}"; exit $?
elif [[ "${1:-}" == "--start" ]]; then
  cmd_start "${2:-}"; exit 0
elif [[ "${1:-}" == "--stop" ]]; then
  cmd_stop "${2:-}"; exit 0
elif [[ "${1:-}" == "--restart" ]]; then
  cmd_restart "${2:-}"; exit 0
fi

main "$@"
