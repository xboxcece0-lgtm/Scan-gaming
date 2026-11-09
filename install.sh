#!/usr/bin/env bash
# Installation de Scan Gaming sur un Raspberry Pi (Raspberry Pi OS) ou tout Linux avec systemd.
# Usage : ./install.sh        (relancer le script met à jour la configuration)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
ENV_FILE=/etc/scan-gaming.env
UNIT=/etc/systemd/system/scan-gaming.service

echo "=== Installation de Scan Gaming ==="
command -v python3 >/dev/null || { sudo apt-get update && sudo apt-get install -y python3; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Python 3.9 ou plus est requis."; exit 1; }

# --- mot de passe Livebox (conservé si déjà configuré) ---------------------------------
if sudo test -f "$ENV_FILE" && sudo grep -q '^LIVEBOX_PASSWORD=' "$ENV_FILE"; then
  read -r -p "Livebox déjà configurée. Changer le mot de passe ? (o/N) " change
else
  change=o
fi
if [[ "${change,,}" == o* ]]; then
  read -r -s -p "Mot de passe administrateur de la Livebox (vide = scan réseau sans box) : " box_pwd; echo
  read -r -s -p "Mot de passe pour protéger l'interface web (vide = aucun) : " app_pwd; echo
  esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  sudo install -m 600 -o root -g root /dev/null "$ENV_FILE"
  {
    echo "TZ=Europe/Paris"
    if [[ -n "$box_pwd" ]]; then echo "LIVEBOX_PASSWORD=\"$(esc "$box_pwd")\""; fi
    if [[ -n "$app_pwd" ]]; then echo "APP_PASSWORD=\"$(esc "$app_pwd")\""; fi
  } | sudo tee "$ENV_FILE" >/dev/null
fi

# --- service systemd -------------------------------------------------------------------
mkdir -p "$DIR/data"
sudo tee "$UNIT" >/dev/null <<UNIT
[Unit]
Description=Scan Gaming - temps de connexion des appareils
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
Environment=PORT=80 DB_PATH=$DIR/data/scan-gaming.db PYTHONUNBUFFERED=1
ExecStart=$(command -v python3) $DIR/server/app.py
# autorise l'écoute sur le port 80 sans être root
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable scan-gaming >/dev/null 2>&1
sudo systemctl restart scan-gaming
sleep 3

if systemctl is-active --quiet scan-gaming; then
  ip=$(hostname -I | awk '{print $1}')
  echo
  echo "✅ Scan Gaming tourne et démarrera automatiquement avec le Raspberry Pi."
  echo "   Sur l'iPhone, ouvrez dans Safari :  http://$(hostname).local   (ou http://$ip)"
  echo "   Journal :  journalctl -u scan-gaming -f"
else
  echo "❌ Le service n'a pas démarré. Dernières lignes du journal :"
  sudo journalctl -u scan-gaming -n 20 --no-pager
  exit 1
fi
