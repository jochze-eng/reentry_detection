#!/usr/bin/env bash
# Recurring Target Detection — uninstaller
set -euo pipefail

INSTALL_DIR="${RTD_INSTALL_DIR:-$HOME/recurring_target_detection}"

log()  { printf '\033[1;34m[uninstall]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

[[ -d "$INSTALL_DIR" ]] || die "No installation found at $INSTALL_DIR (set RTD_INSTALL_DIR if it's elsewhere)."

read -r -p "Stop and remove containers at $INSTALL_DIR? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || die "Aborted."

( cd "$INSTALL_DIR" && docker compose down )

read -r -p "Also delete the database volume (ALL configuration, logs, and detection history)? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
    ( cd "$INSTALL_DIR" && docker compose down -v )
    log "Database volume removed."
fi

read -r -p "Also delete the install directory ($INSTALL_DIR)? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
    rm -rf "$INSTALL_DIR"
    log "Removed $INSTALL_DIR."
fi

log "Uninstall complete."
