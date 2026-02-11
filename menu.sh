#!/usr/bin/env bash
set -euo pipefail

SERVICE="backhaul-failover.service"
TIMER="backhaul-failover.timer"

green(){ printf "\033[32m%s\033[0m\n" "$*"; }
red(){ printf "\033[31m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
cyan(){ printf "\033[36m%s\033[0m\n" "$*"; }

pause(){ read -r -p "Press Enter to continue..." _; }

show_status() {
  echo
  cyan "== Status =="
  systemctl status "$SERVICE" --no-pager -l || true
  echo
  systemctl status "$TIMER" --no-pager -l || true
}

show_logs_live() {
  echo
  cyan "== Live Logs (Ctrl+C to exit) =="
  journalctl -u "$SERVICE" -f -o cat
}

show_logs_tail() {
  echo
  cyan "== Last 200 lines =="
  journalctl -u "$SERVICE" -n 200 --no-pager -o cat
}

start_timer() { systemctl start "$TIMER"; green "✅ Timer started: $TIMER"; }
stop_timer() { systemctl stop "$TIMER" || true; green "🛑 Timer stopped: $TIMER"; }
restart_timer() { systemctl restart "$TIMER"; green "🔁 Timer restarted: $TIMER"; }
run_once() { systemctl start "$SERVICE" || true; green "▶️ Ran once: $SERVICE"; }

enable_autostart() { systemctl enable --now "$TIMER" >/dev/null; green "✅ Enabled + started: $TIMER"; }
disable_autostart() { systemctl disable --now "$TIMER" >/dev/null || true; green "🛑 Disabled + stopped: $TIMER"; }

uninstall_all() {
  yellow "This will remove service, timer, and binaries."
  read -r -p "Type YES to uninstall: " ans
  if [[ "$ans" != "YES" ]]; then red "Cancelled."; return; fi

  systemctl disable --now "$TIMER" >/dev/null || true
  systemctl stop "$SERVICE" >/dev/null 2>&1 || true

  rm -f /etc/systemd/system/backhaul-failover.service
  rm -f /etc/systemd/system/backhaul-failover.timer
  rm -f /usr/local/bin/backhaul-failover.sh
  rm -f /usr/local/bin/backhaul-failover-menu

  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true

  green "✅ Uninstalled."
  exit 0
}

while true; do
  clear
  cyan "Backhaul Failover Manager"
  echo "1) Status"
  echo "2) Run once (service)"
  echo "3) Start timer"
  echo "4) Stop timer"
  echo "5) Restart timer"
  echo "6) Logs (tail)"
  echo "7) Logs (live)"
  echo "8) Enable autostart"
  echo "9) Disable autostart"
  echo "10) Uninstall"
  echo "0) Exit"
  echo
  read -r -p "Select: " choice

  case "$choice" in
    1) show_status; pause ;;
    2) run_once; pause ;;
    3) start_timer; pause ;;
    4) stop_timer; pause ;;
    5) restart_timer; pause ;;
    6) show_logs_tail; pause ;;
    7) show_logs_live ;;
    8) enable_autostart; pause ;;
    9) disable_autostart; pause ;;
    10) uninstall_all ;;
    0) exit 0 ;;
    *) red "Invalid option"; pause ;;
  esac
done
