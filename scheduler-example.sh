#!/usr/bin/env bash
set -euo pipefail

# Best-effort scheduler example.
# This is unsuitable for sub-five-minute windows. Use the systemd daemon for
# the production 30-second refresh cadence.

PROJECT_DIR="/opt/market-monitor"
PYTHON="${PROJECT_DIR}/.venv/bin/python3.14"
CONFIG="${PROJECT_DIR}/config/config.yaml"

cd "${PROJECT_DIR}"
exec "${PYTHON}" "${PROJECT_DIR}/main.py" --config "${CONFIG}"
