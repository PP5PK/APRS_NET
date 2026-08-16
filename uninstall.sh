#!/usr/bin/env bash
#
# uninstall.sh - APRS_NET (PKTNET) uninstaller
#
# Removes the PKTNET systemd service and the installed code. Configuration
# (/etc/pktnet) and data (/var/lib/pktnet, including the check-in database)
# are KEPT unless --purge is given.
#
# Handles both layouts:
#   - centralised install in /opt/apnet (+ symlink in /usr/local/bin)
#   - legacy scattered copies in /usr/local/bin
#
# Usage:
#   sudo ./uninstall.sh [--purge] [-y|--yes] [-n|--dry-run] [--dir DIR]
#
set -euo pipefail

# --- defaults -------------------------------------------------------------- #
INSTALL_DIR="/opt/apnet"
SERVICE="pktnet.service"
UNIT_PATH="/etc/systemd/system/${SERVICE}"
CONF_DIR="/etc/pktnet"
DATA_DIR="/var/lib/pktnet"
BIN_DIR="/usr/local/bin"
SVC_USER="pktnet"

PURGE=0
ASSUME_YES=0
DRY_RUN=0

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
${C_BOLD}APRS_NET uninstaller${C_RST}

Usage: sudo $0 [options]

Options:
  --purge         Also remove configuration (${CONF_DIR}), data
                  (${DATA_DIR}, INCLUDING the check-in database) and the
                  '${SVC_USER}' system user. Destructive.
  -y, --yes       Do not ask for confirmation.
  -n, --dry-run   Show what would be done without changing anything.
  --dir DIR       Centralised install directory (default: ${INSTALL_DIR}).
  -h, --help      Show this help.

By default, config and data are KEPT; only the service and code are removed.
EOF
}

# run a mutating command, honouring --dry-run
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    %s(dry)%s %s\n' "$C_WARN" "$C_RST" "$*"
  else
    "$@"
  fi
}

# --- parse arguments ------------------------------------------------------- #
while [ $# -gt 0 ]; do
  case "$1" in
    --purge)   PURGE=1 ;;
    -y|--yes)  ASSUME_YES=1 ;;
    -n|--dry-run) DRY_RUN=1 ;;
    --dir)     INSTALL_DIR="${2:?--dir needs a path}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) warn "Unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

# --- must be root (except in dry-run) -------------------------------------- #
if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  warn "This script must be run as root (use sudo)."
  exit 1
fi

# --- show plan ------------------------------------------------------------- #
echo
info "APRS_NET uninstaller"
step "service unit : ${UNIT_PATH}"
step "install dir  : ${INSTALL_DIR}"
step "bin scripts  : ${BIN_DIR}/pktnet_bot.py, ${BIN_DIR}/pktnet_cert.py,"
step "               ${BIN_DIR}/pktnet_cert (symlink), ${BIN_DIR}/pktnet_radio.png"
if [ "$PURGE" -eq 1 ]; then
  warn "PURGE enabled: will also remove:"
  step "config       : ${CONF_DIR}"
  step "data + DB    : ${DATA_DIR}   ${C_WARN}(check-in history will be lost)${C_RST}"
  step "system user  : ${SVC_USER}"
else
  step "config/data  : KEPT (${CONF_DIR}, ${DATA_DIR})"
fi
[ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN: nothing will actually change."
echo

# --- confirm --------------------------------------------------------------- #
if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  prompt="Proceed?"
  [ "$PURGE" -eq 1 ] && prompt="Proceed and PERMANENTLY DELETE config and data?"
  read -r -p "$prompt [y/N] " ans
  case "${ans:-N}" in
    y|Y|yes|YES) ;;
    *) info "Aborted."; exit 0 ;;
  esac
fi

# --- 1) service ------------------------------------------------------------ #
if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}"; then
    info "Stopping and disabling ${SERVICE}"
    run systemctl disable --now "${SERVICE}"
  elif systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
    info "Stopping ${SERVICE}"
    run systemctl stop "${SERVICE}"
  else
    step "service not registered, skipping stop/disable"
  fi
  if [ -f "${UNIT_PATH}" ]; then
    info "Removing unit ${UNIT_PATH}"
    run rm -f "${UNIT_PATH}"
    run systemctl daemon-reload
    run systemctl reset-failed "${SERVICE}" 2>/dev/null || true
  else
    step "unit file not present, skipping"
  fi
else
  warn "systemctl not found; skipping service removal"
fi

# --- 2) code --------------------------------------------------------------- #
# Centralised install dir: only remove if it looks like our project.
if [ -d "${INSTALL_DIR}" ]; then
  if [ -f "${INSTALL_DIR}/pktnet_bot.py" ] || [ -f "${INSTALL_DIR}/pktnet_cert.py" ]; then
    info "Removing install directory ${INSTALL_DIR}"
    run rm -rf "${INSTALL_DIR}"
  else
    warn "${INSTALL_DIR} exists but has no PKTNET files; leaving it untouched"
  fi
else
  step "install directory not present, skipping"
fi

# Legacy / convenience entries in bin
for f in pktnet_bot.py pktnet_cert.py pktnet_bot pktnet_cert pktnet_radio.png; do
  target="${BIN_DIR}/${f}"
  if [ -e "${target}" ] || [ -L "${target}" ]; then
    info "Removing ${target}"
    run rm -f "${target}"
  fi
done

# --- 3) purge (config, data, user) ----------------------------------------- #
if [ "$PURGE" -eq 1 ]; then
  if [ -d "${CONF_DIR}" ]; then
    info "Removing config ${CONF_DIR}"
    run rm -rf "${CONF_DIR}"
  fi
  if [ -d "${DATA_DIR}" ]; then
    warn "Removing data ${DATA_DIR} (check-in database included)"
    run rm -rf "${DATA_DIR}"
  fi
  if getent passwd "${SVC_USER}" >/dev/null 2>&1; then
    info "Removing system user ${SVC_USER}"
    run userdel "${SVC_USER}" 2>/dev/null || warn "could not remove user ${SVC_USER}"
  fi
  if getent group "${SVC_USER}" >/dev/null 2>&1; then
    run groupdel "${SVC_USER}" 2>/dev/null || true
  fi
fi

# --- summary --------------------------------------------------------------- #
echo
if [ "$DRY_RUN" -eq 1 ]; then
  info "Dry-run complete. No changes were made."
else
  info "Uninstall complete."
  if [ "$PURGE" -eq 0 ]; then
    step "Kept config: ${CONF_DIR}"
    step "Kept data:   ${DATA_DIR}"
    step "Remove them later with: sudo $0 --purge"
  fi
fi
