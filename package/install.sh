#!/usr/bin/env bash
# Recurring Target Detection — offline installer
# Loads pre-built Docker images from ./images and starts the service via docker compose.
# No internet access is required on the target machine.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${RTD_INSTALL_DIR:-$HOME/recurring_target_detection}"
APP_UID=100
APP_GID=101

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- #
# 1. Preflight checks
# ---------------------------------------------------------------- #
log "Checking prerequisites..."

command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine first: https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || die "Docker daemon is not running or current user lacks permission (try: sudo usermod -aG docker \$USER, then re-login)."
docker compose version >/dev/null 2>&1 || die "The 'docker compose' plugin is not available. Install docker-compose-plugin."

[[ -f "$SCRIPT_DIR/images/rtd-app.tar.gz" ]] || die "Missing $SCRIPT_DIR/images/rtd-app.tar.gz — is this the full installer package?"
[[ -f "$SCRIPT_DIR/images/rtd-postgres.tar.gz" ]] || die "Missing $SCRIPT_DIR/images/rtd-postgres.tar.gz — is this the full installer package?"

for port in 8088 5434; do
    if (command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ":${port} ") \
       || (command -v lsof >/dev/null 2>&1 && lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1); then
        warn "Port ${port} already appears to be in use. Installation may fail to bind it."
    fi
done

if [[ -d "$INSTALL_DIR" ]] && docker inspect recurring_target_detection >/dev/null 2>&1; then
    warn "An existing installation was detected at $INSTALL_DIR."
    read -r -p "Stop and upgrade it in place? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "Aborted by user."
    ( cd "$INSTALL_DIR" && docker compose down ) || true
fi

# ---------------------------------------------------------------- #
# 2. Load pre-built images (no internet / registry pull needed)
# ---------------------------------------------------------------- #
log "Loading application image (this may take a minute)..."
docker load -i "$SCRIPT_DIR/images/rtd-app.tar.gz"

log "Loading database image..."
docker load -i "$SCRIPT_DIR/images/rtd-postgres.tar.gz"

# ---------------------------------------------------------------- #
# 3. Lay out the install directory
# ---------------------------------------------------------------- #
log "Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/certs"
cp "$SCRIPT_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"

# ---------------------------------------------------------------- #
# 4. Self-signed TLS certificate (fresh per install)
# ---------------------------------------------------------------- #
if [[ ! -f "$INSTALL_DIR/certs/server.crt" || ! -f "$INSTALL_DIR/certs/server.key" ]]; then
    if command -v openssl >/dev/null 2>&1; then
        log "Generating self-signed TLS certificate..."
        openssl req -x509 -nodes -days 36500 -newkey rsa:2048 \
            -keyout "$INSTALL_DIR/certs/server.key" \
            -out "$INSTALL_DIR/certs/server.crt" \
            -subj "/C=SG/O=Vaidio/CN=localhost" 2>/dev/null
    elif [[ -f "$SCRIPT_DIR/certs/server.crt" ]]; then
        warn "openssl not found — using bundled fallback certificate (shared across all sites, not unique to this host)."
        cp "$SCRIPT_DIR/certs/server.crt" "$SCRIPT_DIR/certs/server.key" "$INSTALL_DIR/certs/"
    else
        die "openssl is not available and no fallback certificate was bundled. Cannot provision HTTPS."
    fi
fi

# ---------------------------------------------------------------- #
# 5. Fix log volume ownership for the container's non-root user
#    (appuser uid=100 gid=101 inside the image — see Dockerfile)
# ---------------------------------------------------------------- #
if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "${APP_UID}:${APP_GID}" "$INSTALL_DIR/logs" \
        || warn "chown failed. The app container may fail to write logs."
elif command -v sudo >/dev/null 2>&1; then
    sudo chown -R "${APP_UID}:${APP_GID}" "$INSTALL_DIR/logs" \
        || warn "sudo chown failed (no password available?). Falling back to chmod 777 on $INSTALL_DIR/logs so the container can still write to it."
    # Re-check: sudo may have exited 0 but not actually changed ownership in odd environments.
    owner_gid="$(stat -c '%u:%g' "$INSTALL_DIR/logs" 2>/dev/null || stat -f '%u:%g' "$INSTALL_DIR/logs" 2>/dev/null || echo "")"
    [[ "$owner_gid" == "${APP_UID}:${APP_GID}" ]] || chmod 777 "$INSTALL_DIR/logs" 2>/dev/null || true
else
    warn "Cannot chown $INSTALL_DIR/logs to ${APP_UID}:${APP_GID} (no root/sudo). Falling back to chmod 777 so the container can still write logs."
    chmod 777 "$INSTALL_DIR/logs" 2>/dev/null || true
fi

# ---------------------------------------------------------------- #
# 6. Start the stack
# ---------------------------------------------------------------- #
log "Starting containers..."
( cd "$INSTALL_DIR" && docker compose up -d )

log "Waiting for the service to report healthy..."
status="starting"
for _ in $(seq 1 30); do
    status="$(docker inspect --format='{{.State.Health.Status}}' recurring_target_detection 2>/dev/null || echo starting)"
    [[ "$status" == "healthy" ]] && break
    sleep 2
done

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$HOST_IP" ]] && HOST_IP="<this-machine-ip>"

echo
if [[ "$status" == "healthy" ]]; then
    log "Installation complete."
else
    warn "Service did not report healthy in time. Check: docker logs recurring_target_detection"
fi
cat <<EOF

  Web UI:            https://${HOST_IP}:8088
  Default login:      admin / admin888   (Administrator)
                       operator / operator123 (Operator)
  First login:        you will be prompted to set a new password.

  IMPORTANT — Vaidio connection:
  In Settings, set the Vaidio Base URL to this machine's real IP address
  or LAN hostname (e.g. https://${HOST_IP}), NOT https://localhost.
  The app runs in an isolated Docker network, so "localhost" refers to
  its own container and can never reach the Vaidio AINVR service.

  Install directory:  ${INSTALL_DIR}
  Logs:                ${INSTALL_DIR}/logs/app.log
  To stop:             cd ${INSTALL_DIR} && docker compose down
  To view logs:        docker logs -f recurring_target_detection

EOF
