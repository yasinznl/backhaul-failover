#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_BASE="https://raw.githubusercontent.com/yasinznl/backhaul-failover/main"


BIN_DIR="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"

FAILOVER_BIN="${BIN_DIR}/backhaul-failover.sh"
MENU_BIN="${BIN_DIR}/backhaul-failover-menu"

SERVICE_UNIT="${SYSTEMD_DIR}/backhaul-failover.service"
TIMER_UNIT="${SYSTEMD_DIR}/backhaul-failover.timer"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: Please run as root (use sudo)."
    exit 1
  fi
}

fetch() {
  local url="$1"
  local out="$2"
  curl -fsSL "$url" -o "$out"
}

normalize_bash_file() {
  local f="$1"
  # Fix CRLF + remove UTF-8 BOM + ensure a valid bash shebang
  sed -i 's/\r$//' "$f"
  sed -i '1s/^\xEF\xBB\xBF//' "$f"
  sed -i '1s|^#!.*$|#!/usr/bin/env bash|' "$f"
  # If file somehow starts without a shebang, force it
  if ! head -n1 "$f" | grep -q '^#!'; then
    sed -i '1i #!/usr/bin/env bash' "$f"
  fi
}

validate_bash() {
  local f="$1"
  bash -n "$f" >/dev/null
}

need_root
mkdir -p "$BIN_DIR"

# ---- backhaul-failover.sh ----
fetch "${REPO_RAW_BASE}/backhaul-failover.sh" "${FAILOVER_BIN}.new"
normalize_bash_file "${FAILOVER_BIN}.new"
chmod 755 "${FAILOVER_BIN}.new"
validate_bash "${FAILOVER_BIN}.new"
mv -f "${FAILOVER_BIN}.new" "$FAILOVER_BIN"

# ---- menu ----
fetch "${REPO_RAW_BASE}/menu.sh" "${MENU_BIN}.new"
normalize_bash_file "${MENU_BIN}.new"
chmod 755 "${MENU_BIN}.new"
validate_bash "${MENU_BIN}.new"
mv -f "${MENU_BIN}.new" "$MENU_BIN"

# ---- systemd units ----
fetch "${REPO_RAW_BASE}/systemd/backhaul-failover.service" "${SERVICE_UNIT}.new"
sed -i 's/\r$//' "${SERVICE_UNIT}.new"
mv -f "${SERVICE_UNIT}.new" "$SERVICE_UNIT"

fetch "${REPO_RAW_BASE}/systemd/backhaul-failover.timer" "${TIMER_UNIT}.new"
sed -i 's/\r$//' "${TIMER_UNIT}.new"
mv -f "${TIMER_UNIT}.new" "$TIMER_UNIT"

systemctl daemon-reload
systemctl enable --now backhaul-failover.timer >/dev/null

echo "[+] Installed."
"$MENU_BIN" || true
exit 0
