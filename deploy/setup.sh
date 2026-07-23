#!/usr/bin/env bash
# Bootstraps the TikTok Live Recorder Telegram bot on a fresh Linux VM
# (tested on Ubuntu). Installs dependencies, clones/updates the repo,
# creates a venv and registers a systemd service that keeps the bot
# running (and restarts it on crash / reboot).
set -euo pipefail

REPO_URL="https://github.com/Ebooler/tiktok_recorder_bot"
APP_DIR="/opt/tiktok-recorder-bot"
RUN_USER="${SUDO_USER:-$USER}"

sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git

if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$RUN_USER" git -C "$APP_DIR" pull
else
    sudo git clone "$REPO_URL" "$APP_DIR"
    sudo chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"
fi

cd "$APP_DIR"
sudo -u "$RUN_USER" python3 -m venv .venv
sudo -u "$RUN_USER" .venv/bin/pip install --upgrade pip
sudo -u "$RUN_USER" .venv/bin/pip install -e .

if [ ! -f src/bot_config.json ]; then
    sudo -u "$RUN_USER" cp src/bot_config.example.json src/bot_config.json
    echo "!! Compila $APP_DIR/src/bot_config.json con i tuoi valori reali prima di avviare il bot."
fi

sudo sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__USER__#$RUN_USER#g" \
    deploy/tiktok-bot.service | sudo tee /etc/systemd/system/tiktok-bot.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now tiktok-bot

echo "Fatto. Log: sudo journalctl -u tiktok-bot -f"
