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
sudo apt-get install -y software-properties-common ffmpeg git

# The project requires Python >= 3.11, which Ubuntu 22.04 (jammy) does not
# ship by default.
if ! command -v python3.11 >/dev/null 2>&1; then
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -y
fi
sudo apt-get install -y python3.11 python3.11-venv

if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$RUN_USER" git -C "$APP_DIR" pull
else
    sudo git clone "$REPO_URL" "$APP_DIR"
    sudo chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"
fi

cd "$APP_DIR"
# If a venv already exists but wasn't built with 3.11 (e.g. a previous run
# failed before the deadsnakes install above), recreate it from scratch —
# `python -m venv` does not swap the interpreter of an existing venv.
if [ -d .venv ] && ! .venv/bin/python --version 2>&1 | grep -q "3\.11"; then
    sudo -u "$RUN_USER" rm -rf .venv
fi
sudo -u "$RUN_USER" python3.11 -m venv .venv
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
