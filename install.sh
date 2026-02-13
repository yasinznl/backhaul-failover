#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_BASE="https://raw.githubusercontent.com/yasinznl/backhaul-failover/main"

BIN_DIR="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"

FAILOVER_BIN="${BIN_DIR}/backhaul-failover.sh"
MENU_BIN="${BIN_DIR}/backhaul-failover-menu"
WEB_BIN="${BIN_DIR}/backhaul-failover-web.py"

SERVICE_UNIT="${SYSTEMD_DIR}/backhaul-failover.service"
TIMER_UNIT="${SYSTEMD_DIR}/backhaul-failover.timer"
WEB_UNIT="${SYSTEMD_DIR}/backhaul-failover-web.service"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: Please run as root (use sudo)."
    exit 1
  fi
}

need_cmd() {
  local c="$1"
  command -v "$c" >/dev/null 2>&1 || { echo "ERROR: missing command: $c"; exit 1; }
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

normalize_text_file() {
  local f="$1"
  sed -i 's/\r$//' "$f"
  sed -i '1s/^\xEF\xBB\xBF//' "$f"
}

validate_bash() {
  local f="$1"
  bash -n "$f" >/dev/null
}

install_flask_if_needed() {
  if python3 -c "import flask" >/dev/null 2>&1; then
    return 0
  fi

  echo "[*] Flask not found. Installing..."

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y >/dev/null
    apt-get install -y python3-flask >/dev/null
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-flask >/dev/null
    return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    yum install -y python3-flask >/dev/null
    return 0
  fi

  echo "ERROR: Could not auto-install Flask. Please install Flask for python3."
  exit 1
}

need_root
need_cmd curl
need_cmd systemctl
need_cmd ss
need_cmd journalctl
need_cmd python3

mkdir -p "$BIN_DIR"

echo "[*] Downloading scripts..."

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

# ---- web panel (IMPORTANT: file is in repo root) ----
fetch "${REPO_RAW_BASE}/backhaul-failover-web.py" "${WEB_BIN}.new"
normalize_text_file "${WEB_BIN}.new"
chmod 755 "${WEB_BIN}.new"
python3 -m py_compile "${WEB_BIN}.new" >/dev/null 2>&1 || { echo "ERROR: python syntax error in web file"; exit 1; }
mv -f "${WEB_BIN}.new" "$WEB_BIN"

echo "[*] Downloading systemd units..."

# ---- systemd units ----
fetch "${REPO_RAW_BASE}/systemd/backhaul-failover.service" "${SERVICE_UNIT}.new"
normalize_text_file "${SERVICE_UNIT}.new"
mv -f "${SERVICE_UNIT}.new" "$SERVICE_UNIT"

fetch "${REPO_RAW_BASE}/systemd/backhaul-failover.timer" "${TIMER_UNIT}.new"
normalize_text_file "${TIMER_UNIT}.new"
mv -f "${TIMER_UNIT}.new" "$TIMER_UNIT"

fetch "${REPO_RAW_BASE}/systemd/backhaul-failover-web.service" "${WEB_UNIT}.new"
normalize_text_file "${WEB_UNIT}.new"
mv -f "${WEB_UNIT}.new" "$WEB_UNIT"

install_flask_if_needed

systemctl daemon-reload
systemctl enable --now backhaul-failover.timer >/dev/null
systemctl enable --now backhaul-failover-web.service >/dev/null

ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP')"
port="$(systemctl show backhaul-failover-web.service -p Environment --value 2>/dev/null \
  | tr ' ' '\n' | awk -F= '$1=="BH_WEB_PORT"{print $2}' | tail -n1)"
[[ -n "${port:-}" ]] || port="8088"

echo "[+] Installed."
echo
echo "Menu:  sudo ${MENU_BIN}"
echo "Web:   http://${ip}:${port}   (default user/pass: admin/admin)"
echo
exit 0
