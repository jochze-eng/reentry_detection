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
  3. Activate the license (see "Licensing" below). Until a valid license is
     activated the app is locked: monitors will not run and only the License
     page is reachable.
  4. Go to Settings and set the Vaidio Base URL to this machine's real IP
     address (e.g. https://<machine-ip>) — NOT https://localhost. The app
     runs in its own Docker network and "localhost" from inside its
     container can never reach the Vaidio AINVR service on the host.
  5. Configure cameras, thresholds, and enable the LPR/FR monitors.

Licensing (offline, node-locked to this machine):
  The installer records this host's machine fingerprint automatically to
  data/host.fingerprint — no internet is used at any step.
  1. Log in as an Administrator and open the "License" page.
  2. Copy the machine fingerprint (or download the .req file) and send it to
     your Vaidio vendor contact.
  3. The vendor returns a license key issued for this exact machine.
  4. Paste the key (or load the file) on the License page and click Activate.
     The monitors start automatically once activation succeeds.
  Note: a license is bound to this machine and cannot be reused on another
  host. Upgrades preserve data/host.fingerprint, so the license stays valid.

Upgrade:
  Re-run ./install.sh from a newer package build. It detects the existing
  installation, stops it, loads the new images, and restarts — the database
  volume (config, logs, history) is preserved.

Uninstall:
  ./uninstall.sh
