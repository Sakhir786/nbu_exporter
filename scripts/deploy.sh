#!/usr/bin/env bash
set -euo pipefail

# Usage: REPO_URL=https://github.com/<USER>/<REPO_NAME>.git ./scripts/deploy.sh

: "${REPO_URL:?Set REPO_URL to your repository clone URL}"
BUILD_DIR="${BUILD_DIR:-$HOME/nbu-exporter-build}"
INSTALL_DIR="/opt/nbu-exporter"

# Stop any existing NetBackup exporter
sudo systemctl stop nbu_exporter 2>/dev/null || true
sudo systemctl stop nbu-exporter 2>/dev/null || true

ensure_python() {
    if command -v python3.11 >/dev/null; then PY=python3.11; return; fi
    if command -v python3.10 >/dev/null; then PY=python3.10; return; fi
    if command -v python3.12 >/dev/null; then PY=python3.12; return; fi
    if command -v dnf >/dev/null; then
        sudo dnf install -y python3.11 python3.11-pip git
    elif command -v apt-get >/dev/null; then
        sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip git
    elif command -v zypper >/dev/null; then
        sudo zypper install -y python311 python311-pip git
    else
        echo "Cannot detect package manager. Install Python 3.10+ manually." >&2
        exit 1
    fi
    PY=$(command -v python3.11 || command -v python3.10 || command -v python3.12 || command -v python3)
}

ensure_python
echo "Using $PY ($($PY --version))"

# Service user
id nbu-exporter &>/dev/null || sudo useradd -r -s /sbin/nologin nbu-exporter
sudo mkdir -p /var/log/nbu-exporter /etc/nbu-exporter "$INSTALL_DIR"
sudo chown nbu-exporter:nbu-exporter /var/log/nbu-exporter

# Build source
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
[ -d nbu-exporter ] && rm -rf nbu-exporter
git clone "$REPO_URL" nbu-exporter
cd nbu-exporter

# Build wheel in a local venv
"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip wheel --no-deps -w dist .

# System install: dedicated venv under /opt
sudo "$PY" -m venv "$INSTALL_DIR/venv"
sudo "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo "$INSTALL_DIR/venv/bin/pip" install dist/*.whl

# Symlink into /usr/local/bin for a friendly path
sudo ln -sf "$INSTALL_DIR/venv/bin/nbu-exporter" /usr/local/bin/nbu-exporter

# Config (preserve existing; install example if none)
if [ ! -f /etc/nbu-exporter/config.yaml ]; then
    sudo install -m 0640 -o nbu-exporter -g nbu-exporter config.yaml.example /etc/nbu-exporter/config.yaml
    echo ""
    echo "============================================================"
    echo "EDIT /etc/nbu-exporter/config.yaml WITH YOUR NBU MASTER"
    echo "DETAILS AND API KEY, THEN:"
    echo "  sudo systemctl enable --now nbu-exporter"
    echo "============================================================"
    exit 0
fi

# Systemd
sudo install -m 0644 systemd/nbu-exporter.service /etc/systemd/system/nbu-exporter.service
sudo systemctl daemon-reload
sudo systemctl enable --now nbu-exporter

sleep 3
sudo systemctl status nbu-exporter --no-pager
