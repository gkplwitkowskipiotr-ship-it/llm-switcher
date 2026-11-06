#!/bin/bash
set -e

PORT="${1:-3001}"
SERVICE_SRC="/home/pwit/llm-switcher/llm-switcher.service"
SERVICE_DST="/etc/systemd/system/llm-switcher.service"

echo "Instalacja usługi systemowej llm-switcher (port ${PORT})..."
sed "s/Environment=\"PORT=3001\"/Environment=\"PORT=${PORT}\"/" "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now llm-switcher.service
sudo systemctl restart llm-switcher.service

IP=$(hostname -I | awk '{print $1}')
echo
echo "Gotowe! Usługa działa i startuje automatycznie z systemem."
echo "Panel Web: http://${IP}:${PORT}"
echo
sudo systemctl status llm-switcher.service --no-pager
