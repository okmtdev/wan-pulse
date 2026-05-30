#!/usr/bin/env bash
#
# Install wan-pulse as a systemd service on Linux (e.g. Raspberry Pi + Ubuntu).
# It generates a unit file tailored to *this* checkout (user / repo dir / venv),
# installs it, and enables it to start on boot.
#
# Usage (from the repo root):
#   sudo ./deploy/install-systemd.sh
#
set -euo pipefail

SERVICE_NAME="wan-pulse"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo ./deploy/install-systemd.sh" >&2
  exit 1
fi

# Repo dir = parent of this script's dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Run as the human who invoked sudo, never as root.
RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "${RUN_USER}" = "root" ]; then
  echo "Refusing to install a service that runs as root." >&2
  echo "Log in as a normal user and run:  sudo ./deploy/install-systemd.sh" >&2
  exit 1
fi

# Prefer the repo's venv console script; fall back to PATH.
if [ -x "${REPO_DIR}/.venv/bin/wan-pulse" ]; then
  EXEC="${REPO_DIR}/.venv/bin/wan-pulse"
else
  EXEC="$(command -v wan-pulse || true)"
fi
if [ -z "${EXEC}" ]; then
  echo "Could not find the 'wan-pulse' command." >&2
  echo "Install it first, ideally in a venv:  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

echo "Service user : ${RUN_USER}"
echo "Working dir  : ${REPO_DIR}"
echo "Executable   : ${EXEC}"

# Mic access requires membership in the 'audio' group.
if ! id -nG "${RUN_USER}" | tr ' ' '\n' | grep -qx audio; then
  echo "Adding '${RUN_USER}' to the 'audio' group (needed for microphone access)."
  usermod -aG audio "${RUN_USER}"
fi

cat > "${UNIT_PATH}" <<EOF
[Unit]
Description=wan-pulse - record dog barks from the microphone
Documentation=https://github.com/okmtdev/wan-pulse
After=sound.target network-online.target
Wants=sound.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${EXEC} run
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "Wrote ${UNIT_PATH}"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

cat <<EOF

Done. wan-pulse is now running and will start on boot.

Useful commands:
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f          # follow logs
  sudo systemctl restart ${SERVICE_NAME}    # after editing code / wan-pulse.toml
  sudo systemctl stop ${SERVICE_NAME}
  sudo systemctl disable ${SERVICE_NAME}    # stop starting on boot

Tip: run 'wan-pulse init' and tune ${REPO_DIR}/wan-pulse.toml first,
then restart the service.
EOF
