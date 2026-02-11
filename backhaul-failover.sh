#!/usr/bin/env bash
set -euo pipefail

SERVICE_GLOB="backhaul-iran*.service"

FAIL_THRESHOLD=3
STATE_FILE="/run/backhaul-failover.state"

FATAL_WINDOW="10 minutes ago"
FATAL_REGEX="(panic|fatal|segfault|address already in use|bind failed|cannot bind|permission denied)"

CANDIDATE_WAIT_SEC=30
CANDIDATE_POLL_SEC=3

LOCKFILE="/run/backhaul-failover.lock"
exec 9>"$LOCKFILE"
flock -n 9 || exit 0

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
  systemctl list-units --type=service --all "$SERVICE_GLOB" --no-legend --no-pager 2>/dev/null \
    | awk '{print $1}' | sed '/^$/d'
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
  journalctl -u "$svc" --since "$FATAL_WINDOW" --no-pager 2>/dev/null | grep -Eiq "$FATAL_REGEX"
}

# Real-time health: must have ESTABLISHED TCP session to bind port
has_established_now() {
  local port="$1"
  ss -nt state established 2>/dev/null | grep -Eq "[:.]${port}\b"
}

is_healthy() {
  local svc="$1" bind_port="$2"
  is_active "$svc" || return 1
  is_listening "$bind_port" || return 1
  has_fatal_errors_recently "$svc" && return 1
  has_established_now "$bind_port" || return 1
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

  log_info "Primary=$primary :$pport | Backups=${#backups[@]} | Threshold=${FAIL_THRESHOLD}"

  if is_healthy "$primary" "$pport"; then
    log_ok "Primary healthy: $primary (:$pport)"
    state_reset

    # Keep only one active service (policy)
    for b in "${backups[@]}"; do
      if is_active "$b"; then
        log_warn "Stopping backup (policy): $b"
        systemctl stop "$b" >/dev/null 2>&1 || true
        log_ok "Backup stopped: $b"
      fi
    done
    exit 0
  fi

  # Anti-flap counter (per primary)
  local last_svc last_cnt
  last_svc="$(state_read | awk '{print $1}' 2>/dev/null || true)"
  last_cnt="$(state_read | awk '{print $2}' 2>/dev/null || true)"
  [[ -n "${last_cnt:-}" ]] || last_cnt=0
  if [[ "$last_svc" != "$primary" ]]; then last_cnt=0; fi
  last_cnt=$((last_cnt + 1))
  state_write "$primary" "$last_cnt"

  log_bad "Primary unhealthy: $primary (:$pport) [count=${last_cnt}/${FAIL_THRESHOLD}]"
  if [[ "$last_cnt" -lt "$FAIL_THRESHOLD" ]]; then
    log_warn "Failover suppressed (anti-flap). Waiting for next check."
    exit 0
  fi

  # Try backups; do NOT stop primary unless a backup becomes healthy
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
    systemctl stop "$b" >/dev/null 2>&1 || true
  done

  if [[ -z "${chosen:-}" ]]; then
    log_bad "No healthy backup found. Keeping primary running."
    exit 0
  fi

  log_switch "Switching: stopping primary: $primary"
  systemctl stop "$primary" >/dev/null 2>&1 || true
  log_bad "Primary stopped: $primary"

  for b in "${backups[@]}"; do
    [[ "$b" == "$chosen" ]] && continue
    if is_active "$b"; then
      log_warn "Stopping non-chosen backup: $b"
      systemctl stop "$b" >/dev/null 2>&1 || true
    fi
  done

  state_reset
}

main "$@"
