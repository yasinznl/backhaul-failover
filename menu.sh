#!/usr/bin/env bash
set -euo pipefail

SERVICE="backhaul-failover.service"
TIMER="backhaul-failover.timer"
FAILOVER_BIN="/usr/local/bin/backhaul-failover.sh"

WEB_SERVICE="backhaul-failover-web.service"

# -------- UI helpers --------
have_tput=0
command -v tput >/dev/null 2>&1 && have_tput=1

if [[ "$have_tput" == "1" && -t 1 ]]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; RESET="$(tput sgr0)"
  RED="$(tput setaf 1)"; GREEN="$(tput setaf 2)"; YEL="$(tput setaf 3)"; CYA="$(tput setaf 6)"; WHT="$(tput setaf 7)"
else
  BOLD=""; DIM=""; RESET=""
  RED=""; GREEN=""; YEL=""; CYA=""; WHT=""
fi

hr() {
  local w
  w=$(tput cols 2>/dev/null || echo 80)
  printf "%*s\n" "$w" "" | tr " " "─"
}

VERSION="2.1"

render_header() {
  clear

  local primary port timer_state web_state

  primary="$("$FAILOVER_BIN" --current 2>/dev/null | awk '{print $1}' || true)"
  port="$("$FAILOVER_BIN" --current 2>/dev/null | awk '{print $2}' || true)"

  if systemctl is-active --quiet "$TIMER"; then
    timer_state="${GREEN}ACTIVE${RESET}"
  else
    timer_state="${RED}INACTIVE${RESET}"
  fi

  if systemctl is-active --quiet "$WEB_SERVICE"; then
    web_state="${GREEN}ACTIVE${RESET}"
  else
    web_state="${RED}INACTIVE${RESET}"
  fi

  echo -e "${CYA}"
  echo "██████╗  █████╗  ██████╗██╗  ██╗██╗  ██╗ █████╗ ██╗   ██╗██╗     "
  echo "██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██║  ██║██╔══██╗██║   ██║██║     "
  echo "██████╔╝███████║██║     █████╔╝ ███████║███████║██║   ██║██║     "
  echo "██╔══██╗██╔══██║██║     ██╔═██╗ ██╔══██║██╔══██║██║   ██║██║     "
  echo "██████╔╝██║  ██║╚██████╗██║  ██╗██║  ██║██║  ██║╚██████╔╝███████╗"
  echo "╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
  echo -e "${RESET}"

  hr
  echo -e "${BOLD}Version:${RESET} ${VERSION}"
  echo -e "${BOLD}Primary:${RESET} ${primary:-None} ${port:+(Port :$port)}"
  echo -e "${BOLD}Timer:${RESET} $timer_state"
  echo -e "${BOLD}Web:${RESET} $web_state"
  hr
}

title(){ render_header; } # backward compatibility

ok(){ echo "${GREEN}✅ $*${RESET}"; }
warn(){ echo "${YEL}🟡 $*${RESET}"; }
bad(){ echo "${RED}❌ $*${RESET}"; }
info(){ echo "${CYA}ℹ️  $*${RESET}"; }

pause(){ echo; read -r -p "Press Enter to continue..." _; }

show_status() {
  echo
  echo "${BOLD}${CYA}== Service status ==${RESET}"
  systemctl status "$SERVICE" --no-pager -l || true
  echo
  echo "${BOLD}${CYA}== Timer status ==${RESET}"
  systemctl status "$TIMER" --no-pager -l || true
  echo
  echo "${BOLD}${CYA}== Web status ==${RESET}"
  systemctl status "$WEB_SERVICE" --no-pager -l || true
}

show_logs_tail() {
  echo
  echo "${BOLD}${CYA}== Last 200 lines ==${RESET}"
  journalctl -u "$SERVICE" -n 200 --no-pager -o cat
}

show_logs_live() {
  echo
  echo "${BOLD}${CYA}== Live logs (Ctrl+C to exit) ==${RESET}"
  journalctl -u "$SERVICE" -f -o cat
}

run_once() {
  systemctl start "$SERVICE" || true
  ok "Ran once: $SERVICE"
}

start_timer() { systemctl start "$TIMER" || true; ok "Timer started: $TIMER"; }
stop_timer() { systemctl stop "$TIMER" || true; ok "Timer stopped: $TIMER"; }
restart_timer() { systemctl restart "$TIMER" || true; ok "Timer restarted: $TIMER"; }

enable_autostart() { systemctl enable --now "$TIMER" >/dev/null; ok "Enabled + started: $TIMER"; }
disable_autostart() { systemctl disable --now "$TIMER" >/dev/null || true; ok "Disabled + stopped: $TIMER"; }

list_tunnels() {
  echo
  echo "${BOLD}${CYA}== Tunnels ==${RESET}"
  "$FAILOVER_BIN" --list || true
}

manual_switch() {
  echo
  echo "${BOLD}${CYA}== Manual switch ==${RESET}"
  "$FAILOVER_BIN" --list || true
  echo
  read -r -p "Enter service name to switch to (e.g. backhaul-iran407.service): " svc
  [[ -n "${svc:-}" ]] || { bad "No service entered."; return; }
  "$FAILOVER_BIN" --switch "$svc" && ok "Switched to $svc" || bad "Switch failed."
}

manage_start_stop_restart() {
  echo
  echo "${BOLD}${CYA}== Manage tunnel ==${RESET}"
  "$FAILOVER_BIN" --list || true
  echo
  read -r -p "Service name: " svc
  [[ -n "${svc:-}" ]] || { bad "No service entered."; return; }

  echo
  echo "1) Start"
  echo "2) Stop"
  echo "3) Restart"
  echo
  read -r -p "Select: " a
  case "$a" in
    1) "$FAILOVER_BIN" --start "$svc" || true; ok "Started: $svc" ;;
    2) "$FAILOVER_BIN" --stop "$svc" || true; ok "Stopped: $svc" ;;
    3) "$FAILOVER_BIN" --restart "$svc" || true; ok "Restarted: $svc" ;;
    *) bad "Invalid option" ;;
  esac
}

traffic_primary_live() {
  echo
  echo "${BOLD}${CYA}== Live traffic (Primary) ==${RESET}"

  local cur
  cur="$("$FAILOVER_BIN" --current 2>/dev/null || true)"
  if [[ -z "${cur:-}" ]]; then
    bad "No active primary detected."
    return
  fi

  local svc port
  svc="$(awk '{print $1}' <<<"$cur")"
  port="$(awk '{print $2}' <<<"$cur")"

  info "Primary: ${BOLD}${svc}${RESET}  Port: ${BOLD}:${port}${RESET}"
  echo "${DIM}Tip: Drop-based failover checks 10m traffic vs previous 10m.${RESET}"
  echo
  "$FAILOVER_BIN" --watch-traffic "$port" 1
}

traffic_choose_live() {
  echo
  echo "${BOLD}${CYA}== Live traffic (Choose tunnel) ==${RESET}"
  "$FAILOVER_BIN" --list || true
  echo
  read -r -p "Enter service name (e.g. backhaul-iran407.service): " svc
  [[ -n "${svc:-}" ]] || { bad "No service entered."; return; }

  local line port
  line="$("$FAILOVER_BIN" --list 2>/dev/null | awk -v s="$svc" '$1==s {print $0}' | head -n1 || true)"
  port="$(awk '{print $3}' <<<"$line" 2>/dev/null || true)"

  if [[ -z "${port:-}" || "$port" == "-" ]]; then
    bad "Could not determine port for: $svc"
    return
  fi

  info "Service: ${BOLD}${svc}${RESET}  Port: ${BOLD}:${port}${RESET}"
  echo
  "$FAILOVER_BIN" --watch-traffic "$port" 1
}

# ---- Web panel helpers ----
web_status() { systemctl status "$WEB_SERVICE" --no-pager -l || true; }
web_restart() { systemctl restart "$WEB_SERVICE" || true; ok "Web panel restarted: $WEB_SERVICE"; }
web_start() { systemctl start "$WEB_SERVICE" || true; ok "Web panel started: $WEB_SERVICE"; }
web_stop() { systemctl stop "$WEB_SERVICE" || true; ok "Web panel stopped: $WEB_SERVICE"; }

web_show_addr() {
  local ip port
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP')"
  port="$(systemctl show "$WEB_SERVICE" -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | awk -F= '$1=="BH_WEB_PORT"{print $2}' | tail -n1)"
  [[ -n "${port:-}" ]] || port="8088"
  info "Web: http://${ip}:${port}  (default user/pass: admin/admin)"
}

uninstall_all() {
  warn "This will remove service, timer, web panel, and binaries."
  read -r -p "Type YES to uninstall: " ans
  [[ "$ans" == "YES" ]] || { bad "Cancelled."; return; }

  systemctl disable --now "$TIMER" >/dev/null || true
  systemctl stop "$SERVICE" >/dev/null 2>&1 || true

  systemctl disable --now "$WEB_SERVICE" >/dev/null || true
  systemctl stop "$WEB_SERVICE" >/dev/null 2>&1 || true

  rm -f /etc/systemd/system/backhaul-failover.service
  rm -f /etc/systemd/system/backhaul-failover.timer
  rm -f /etc/systemd/system/backhaul-failover-web.service
  rm -f /usr/local/bin/backhaul-failover.sh
  rm -f /usr/local/bin/backhaul-failover-menu
  rm -f /usr/local/bin/backhaul-failover-web.py

  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true
  ok "Uninstalled."
  exit 0
}

while true; do
  render_header

  echo "${BOLD}${WHT}Monitor${RESET}"
  echo "  1) Status"
  echo "  2) Run once (service)"
  echo "  3) Logs (tail)"
  echo "  4) Logs (live)"
  echo
  echo "${BOLD}${WHT}Scheduler${RESET}"
  echo "  5) Start timer"
  echo "  6) Stop timer"
  echo "  7) Restart timer"
  echo "  8) Enable autostart"
  echo "  9) Disable autostart"
  echo
  echo "${BOLD}${WHT}Tunnels${RESET}"
  echo "  10) List tunnels"
  echo "  11) Manual switch (pick service)"
  echo "  12) Manage tunnel (start/stop/restart)"
  echo
  echo "${BOLD}${WHT}Traffic${RESET}"
  echo "  13) Live traffic (Primary)"
  echo "  14) Live traffic (Choose tunnel)"
  echo
  echo "${BOLD}${WHT}Web Panel${RESET}"
  echo "  15) Web status"
  echo "  16) Web start"
  echo "  17) Web stop"
  echo "  18) Web restart"
  echo "  19) Show web address"
  echo
  echo "${BOLD}${WHT}System${RESET}"
  echo "  20) Uninstall"
  echo "  0) Exit"
  echo
  read -r -p "Select: " choice

  case "$choice" in
    1) show_status; pause ;;
    2) run_once; pause ;;
    3) show_logs_tail; pause ;;
    4) show_logs_live ;;
    5) start_timer; pause ;;
    6) stop_timer; pause ;;
    7) restart_timer; pause ;;
    8) enable_autostart; pause ;;
    9) disable_autostart; pause ;;
    10) list_tunnels; pause ;;
    11) manual_switch; pause ;;
    12) manage_start_stop_restart; pause ;;
    13) traffic_primary_live ;;
    14) traffic_choose_live ;;
    15) web_status; pause ;;
    16) web_start; pause ;;
    17) web_stop; pause ;;
    18) web_restart; pause ;;
    19) web_show_addr; pause ;;
    20) uninstall_all ;;
    0) exit 0 ;;
    *) bad "Invalid option"; pause ;;
  esac
done
