#!/usr/bin/env bash
#
# install.sh - APRS_NET (PKTNET) installer
#
# Centralised install:
#   code   -> /opt/APRS_NET           (single source of truth)
#   config -> /etc/pktnet          (FHS: host configuration)
#   data   -> /var/lib/pktnet      (FHS: application state / database)
#   service-> /etc/systemd/system/pktnet.service (generated with correct paths)
#   cli    -> /usr/local/bin/pktnet_bot, pktnet_cert (symlinks, not copies)
#
# Safe to re-run (upgrade): code is refreshed; existing config and data are
# preserved. Run from the project directory (the git clone).
#
# Usage:
#   sudo ./install.sh [--dir DIR] [--no-deps] [--no-start] [-y] [-n|--dry-run]
#
set -euo pipefail

# --- defaults -------------------------------------------------------------- #
INSTALL_DIR="/opt/APRS_NET"
SERVICE="pktnet.service"
UNIT_PATH="/etc/systemd/system/${SERVICE}"
CONF_DIR="/etc/pktnet"
CONF_FILE="${CONF_DIR}/pktnet.conf"
DATA_DIR="/var/lib/pktnet"
BIN_DIR="/usr/local/bin"
SVC_USER="pktnet"

INSTALL_DEPS=1
START_SERVICE=1
ASSUME_YES=0
DRY_RUN=0

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FRESH=0

# --- output helpers (amber/blue, no red/green semantics) ------------------- #
if [ -t 1 ]; then
  C_INFO=$'\e[38;5;39m'; C_WARN=$'\e[38;5;214m'; C_DIM=$'\e[2m'
  C_BOLD=$'\e[1m'; C_RST=$'\e[0m'
else
  C_INFO=; C_WARN=; C_DIM=; C_BOLD=; C_RST=
fi
info() { printf '%s[*]%s %s\n' "$C_INFO" "$C_RST" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_RST" "$*"; }
step() { printf '%s ->%s %s\n' "$C_DIM" "$C_RST" "$*"; }

usage() {
  cat <<EOF
${C_BOLD}APRS_NET installer${C_RST}

Usage: sudo $0 [options]

Options:
  --dir DIR     Install code to DIR (default: ${INSTALL_DIR}).
  --no-deps     Skip installing python3-reportlab (needed for certificates).
  --no-start    Install/enable the service but do not start it now.
  -y, --yes     Do not ask for confirmation.
  -n, --dry-run Show what would be done without changing anything.
  -h, --help    Show this help.

Config (${CONF_FILE}) and data (${DATA_DIR}) are preserved on re-run.
EOF
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    %s(dry)%s %s\n' "$C_WARN" "$C_RST" "$*"
  else
    "$@"
  fi
}

# write a heredoc file, honouring --dry-run (content on stdin)
write_file() {
  local dest="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    %s(dry)%s write %s\n' "$C_WARN" "$C_RST" "$dest"
    cat >/dev/null
  else
    cat > "$dest"
  fi
}

# --- parse arguments ------------------------------------------------------- #
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)      INSTALL_DIR="${2:?--dir needs a path}"; shift ;;
    --no-deps)  INSTALL_DEPS=0 ;;
    --no-start) START_SERVICE=0 ;;
    -y|--yes)   ASSUME_YES=1 ;;
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help)  usage; exit 0 ;;
    *) warn "Unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

# --- sanity: must run from the project dir --------------------------------- #
for required in pktnet_bot.py pktnet_cert.py pktnet.conf.example; do
  if [ ! -f "${SRC_DIR}/${required}" ]; then
    warn "Missing '${required}' in ${SRC_DIR}."
    warn "Run this from the project directory (the git clone)."
    exit 1
  fi
done

# --- must be root (except in dry-run) -------------------------------------- #
if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  warn "This script must be run as root (use sudo)."
  exit 1
fi

SAME_DIR=0
[ "$SRC_DIR" = "$INSTALL_DIR" ] && SAME_DIR=1

# --- plan ------------------------------------------------------------------ #
echo
info "APRS_NET installer"
step "source dir  : ${SRC_DIR}"
step "install dir : ${INSTALL_DIR}$( [ "$SAME_DIR" -eq 1 ] && echo '  (in place, no copy)')"
step "config      : ${CONF_FILE}$( [ -f "$CONF_FILE" ] && echo '  (exists, kept)')"
step "data        : ${DATA_DIR}"
step "service     : ${UNIT_PATH}"
step "cli symlinks: ${BIN_DIR}/pktnet_bot, ${BIN_DIR}/pktnet_cert"
step "deps        : $( [ "$INSTALL_DEPS" -eq 1 ] && echo 'python3-reportlab' || echo 'skipped' )"
[ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN: nothing will actually change."
echo

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  read -r -p "Proceed? [Y/n] " ans
  case "${ans:-Y}" in
    y|Y|yes|YES|"") ;;
    *) info "Aborted."; exit 0 ;;
  esac
fi

# --- 1) system user -------------------------------------------------------- #
if getent passwd "${SVC_USER}" >/dev/null 2>&1; then
  step "user ${SVC_USER} already exists"
else
  info "Creating system user ${SVC_USER}"
  run useradd --system --no-create-home --shell /usr/sbin/nologin "${SVC_USER}"
fi

# --- 2) code --------------------------------------------------------------- #
info "Installing code to ${INSTALL_DIR}"
run mkdir -p "${INSTALL_DIR}"
if [ "$SAME_DIR" -eq 1 ]; then
  step "running in place, skipping copy"
else
  for f in pktnet_bot.py pktnet_cert.py; do
    run install -m 0755 "${SRC_DIR}/${f}" "${INSTALL_DIR}/${f}"
  done
  if [ -f "${SRC_DIR}/pktnet_radio.png" ]; then
    run install -m 0644 "${SRC_DIR}/pktnet_radio.png" "${INSTALL_DIR}/pktnet_radio.png"
  else
    warn "pktnet_radio.png not found; certificates will render without the background"
  fi
fi
run chmod 0755 "${INSTALL_DIR}/pktnet_bot.py" "${INSTALL_DIR}/pktnet_cert.py"

# --- 3) config ------------------------------------------------------------- #
run mkdir -p "${CONF_DIR}"
if [ -f "${CONF_FILE}" ]; then
  step "config exists, keeping ${CONF_FILE}"
else
  info "Installing config template to ${CONF_FILE}"
  run install -m 0640 "${SRC_DIR}/pktnet.conf.example" "${CONF_FILE}"
  CONFIG_FRESH=1
fi
run chown root:"${SVC_USER}" "${CONF_FILE}" 2>/dev/null || true
run chmod 0640 "${CONF_FILE}" 2>/dev/null || true

# --- 4) data --------------------------------------------------------------- #
info "Preparing data directory ${DATA_DIR}"
run mkdir -p "${DATA_DIR}"
run chown -R "${SVC_USER}":"${SVC_USER}" "${DATA_DIR}"

# --- 5) systemd unit (generated with correct paths) ------------------------ #
info "Writing systemd unit ${UNIT_PATH}"
write_file "${UNIT_PATH}" <<EOF
[Unit]
Description=PKTNET APRS Net check-in bot
Documentation=https://github.com/PP5PK/APRS_NET
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
ExecStart=${INSTALL_DIR}/pktnet_bot.py run --config ${CONF_FILE}
Restart=on-failure
RestartSec=10

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF

# --- 6) cli symlinks ------------------------------------------------------- #
info "Creating CLI symlinks in ${BIN_DIR}"
run ln -sf "${INSTALL_DIR}/pktnet_bot.py"  "${BIN_DIR}/pktnet_bot"
run ln -sf "${INSTALL_DIR}/pktnet_cert.py" "${BIN_DIR}/pktnet_cert"

# --- 7) dependencies ------------------------------------------------------- #
if [ "$INSTALL_DEPS" -eq 1 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    info "Installing python3-reportlab (for certificates)"
    run apt-get install -y python3-reportlab || warn "reportlab install failed; certificates may not render"
  else
    warn "apt-get not found; install reportlab manually for certificates"
  fi
fi

# --- 8) enable / start ----------------------------------------------------- #
if command -v systemctl >/dev/null 2>&1; then
  run systemctl daemon-reload
  info "Enabling ${SERVICE} (start on boot)"
  run systemctl enable "${SERVICE}"
  if [ "$START_SERVICE" -eq 1 ] && [ "$CONFIG_FRESH" -eq 0 ] && [ -f "${CONF_FILE}" ]; then
    info "Starting ${SERVICE}"
    run systemctl restart "${SERVICE}"
  else
    step "not starting yet (set the passcode in ${CONF_FILE} first)"
  fi
else
  warn "systemctl not found; skipping service enable/start"
fi

# --- summary --------------------------------------------------------------- #
echo
if [ "$DRY_RUN" -eq 1 ]; then
  info "Dry-run complete. No changes were made."
  exit 0
fi
info "Install complete."
if [ "$CONFIG_FRESH" -eq 1 ]; then
  warn "Set your passcode before starting the service:"
  step "sudo nano ${CONF_FILE}"
  step "sudo systemctl start ${SERVICE}"
elif [ "$START_SERVICE" -eq 0 ]; then
  step "Service enabled but not started. Start it with:"
  step "sudo systemctl start ${SERVICE}"
fi
step "Logs:      sudo journalctl -u ${SERVICE} -f"
step "Update:    cd ${INSTALL_DIR} && sudo git pull && sudo systemctl restart ${SERVICE}"
step "Events:    sudo pktnet_bot addevent \"PKTNET Net #1\" <start> <end>"
step "Certs:     pktnet_cert -c ${CONF_FILE} --event <id> --out ./certs"
