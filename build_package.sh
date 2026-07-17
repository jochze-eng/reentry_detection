#!/usr/bin/env bash
# Builds the offline installer package for Recurring Target Detection.
# Run this on a machine with Docker + internet access (e.g. your dev machine).
# Target machines that receive the resulting tarball need NO internet access.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_SRC="$REPO_DIR/package"
PLATFORM="linux/amd64"
VERSION="$(date +%Y%m%d-%H%M%S)"
[[ -n "${1:-}" ]] && VERSION="$1"

OUT_NAME="recurring-target-detection-installer-${VERSION}"
BUILD_DIR="$REPO_DIR/dist/${OUT_NAME}"

log() { printf '\033[1;34m[build]\033[0m %s\n' "$1"; }

command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required (Docker Desktop / buildx plugin)." >&2; exit 1; }
command -v skopeo >/dev/null 2>&1 || { echo "skopeo is required to fetch the postgres image as a portable archive (brew install skopeo)." >&2; exit 1; }

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/images"

# NOTE: Docker Desktop's containerd image store has a known bug where
# `docker save` fails with "unable to create manifests file: ... not found"
# on images pulled/built for a non-native --platform. We avoid it entirely:
#   - our own image is exported directly from buildx (-o type=docker,dest=...)
#   - postgres is fetched straight from the registry via skopeo, never
#     touching the local docker image store as a multi-platform manifest.

log "Building application image for ${PLATFORM} and exporting archive directly..."
docker buildx build --platform "$PLATFORM" \
    -t "recurring-target-detection:latest" \
    -f "$REPO_DIR/Dockerfile" \
    -o "type=docker,dest=$BUILD_DIR/images/rtd-app.tar" \
    "$REPO_DIR"

log "Fetching postgres:15-alpine for ${PLATFORM} via skopeo..."
skopeo copy --override-arch amd64 --override-os linux \
    docker://docker.io/library/postgres:15-alpine \
    "docker-archive:$BUILD_DIR/images/rtd-postgres.tar:postgres:15-alpine"

log "Copying package files..."
cp "$PKG_SRC/install.sh" "$PKG_SRC/uninstall.sh" "$PKG_SRC/docker-compose.yml" "$PKG_SRC/README.txt" "$BUILD_DIR/"
chmod +x "$BUILD_DIR/install.sh" "$BUILD_DIR/uninstall.sh"

log "Generating fallback TLS certificate (used only if the target host lacks openssl)..."
mkdir -p "$BUILD_DIR/certs"
openssl req -x509 -nodes -days 36500 -newkey rsa:2048 \
    -keyout "$BUILD_DIR/certs/server.key" \
    -out "$BUILD_DIR/certs/server.crt" \
    -subj "/C=SG/O=Vaidio/CN=localhost" 2>/dev/null

echo "$VERSION" > "$BUILD_DIR/VERSION"
echo "Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$BUILD_DIR/VERSION"
echo "Git commit: $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)" >> "$BUILD_DIR/VERSION"
echo "Platform: ${PLATFORM}" >> "$BUILD_DIR/VERSION"

log "Compressing image archives..."
gzip -f "$BUILD_DIR/images/rtd-app.tar"
gzip -f "$BUILD_DIR/images/rtd-postgres.tar"

log "Creating tarball..."
( cd "$REPO_DIR/dist" && tar czf "${OUT_NAME}.tar.gz" "${OUT_NAME}" )

log "Done: $REPO_DIR/dist/${OUT_NAME}.tar.gz"
du -h "$REPO_DIR/dist/${OUT_NAME}.tar.gz"
