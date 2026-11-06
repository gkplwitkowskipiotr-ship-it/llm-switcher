#!/bin/bash
# Installs llm-switcher as a systemd service for the current user.
#
# Usage: ./install-service.sh [PORT] [SCRIPTS_DIR]
#   PORT         HTTP port (default: 3001)
#   SCRIPTS_DIR  directory scanned for *.sh scripts (default: home directory of the user)
set -euo pipefail

# Real user even when invoked via `sudo ./install-service.sh`
SERVICE_USER="${SUDO_USER:-$(id -un)}"
if [ "$SERVICE_USER" = "root" ]; then
    echo "Uruchom jako zwykły użytkownik (skrypt sam użyje sudo), a nie jako root." >&2
    exit 1
fi
USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"

PORT="${1:-3001}"
SCRIPTS_DIR="$(realpath "${2:-$USER_HOME}")"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

SERVICE_SRC="${APP_DIR}/llm-switcher.service"
SERVICE_DST="/etc/systemd/system/llm-switcher.service"

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo "Nieprawidłowy port: $PORT" >&2
    exit 1
fi
if [ ! -d "$SCRIPTS_DIR" ]; then
    echo "Katalog skryptów nie istnieje: $SCRIPTS_DIR" >&2
    exit 1
fi
if ! id -nG "$SERVICE_USER" | grep -qw docker; then
    echo "Uwaga: użytkownik $SERVICE_USER nie należy do grupy docker (sudo usermod -aG docker $SERVICE_USER)." >&2
fi

echo "Instalacja usługi systemowej llm-switcher"
echo "  użytkownik:      $SERVICE_USER"
echo "  port:            $PORT"
echo "  katalog skryptów: $SCRIPTS_DIR"
echo "  katalog aplikacji: $APP_DIR"

sed -e "s|__USER__|${SERVICE_USER}|g" \
    -e "s|__HOME__|${USER_HOME}|g" \
    -e "s|__PORT__|${PORT}|g" \
    -e "s|__SCRIPTS_DIR__|${SCRIPTS_DIR}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__PYTHON__|${PYTHON}|g" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable llm-switcher.service
sudo systemctl restart llm-switcher.service

IP=$(hostname -I | awk '{print $1}')
echo
echo "Gotowe! Usługa działa i startuje automatycznie z systemem."
echo "Panel Web: http://${IP}:${PORT}"
echo
sudo systemctl status llm-switcher.service --no-pager
