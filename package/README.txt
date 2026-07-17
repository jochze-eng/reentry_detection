Recurring Target Detection — Offline Installer
================================================

Contents:
  install.sh            Run this to install/upgrade the service.
  uninstall.sh           Stops and optionally removes the service/data.
  docker-compose.yml      Compose definition (pre-built images, no build step).
  images/rtd-app.tar.gz         Pre-built application image.
  images/rtd-postgres.tar.gz    Pre-built PostgreSQL image.
  certs/                 Fallback TLS cert used only if openssl is unavailable
                          on the target machine.
  VERSION                 Version/build metadata for this package.

Requirements on the target machine:
  - Linux x86_64 (amd64)
  - Docker Engine + the `docker compose` plugin already installed
  - No internet access required — all images are bundled.

Install:
  tar xzf recurring-target-detection-installer-<version>.tar.gz
  cd recurring-target-detection-installer-<version>
  ./install.sh

  Optional: install to a custom directory:
  RTD_INSTALL_DIR=/opt/recurring_target_detection ./install.sh

After install:
  1. Open https://<machine-ip>:8088
  2. Log in with admin / admin888 (or operator / operator123) and set a new
     password when prompted.
  3. Go to Settings and set the Vaidio Base URL to this machine's real IP
     address (e.g. https://<machine-ip>) — NOT https://localhost. The app
     runs in its own Docker network and "localhost" from inside its
     container can never reach the Vaidio AINVR service on the host.
  4. Configure cameras, thresholds, and enable the LPR/FR monitors.

Upgrade:
  Re-run ./install.sh from a newer package build. It detects the existing
  installation, stops it, loads the new images, and restarts — the database
  volume (config, logs, history) is preserved.

Uninstall:
  ./uninstall.sh
