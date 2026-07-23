# Deploy del bot

Requisiti: una VM Linux (Ubuntu) sempre accesa, **in una regione non ristretta**
(non Italia / Hong Kong / UK — vedi `docs/GUIDE.md`), con accesso SSH.

```bash
git clone https://github.com/Ebooler/tiktok_recorder_bot /opt/tiktok-recorder-bot
cd /opt/tiktok-recorder-bot
sudo bash deploy/setup.sh
```

Lo script installa le dipendenze (Python, ffmpeg), crea un virtualenv, e registra
il bot come servizio systemd (`tiktok-bot`) che riparte da solo in caso di crash
o riavvio del server.

Prima del primo avvio, compila `src/bot_config.json` con `bot_token`, `api_id`,
`api_hash` e `allowed_user_id` (vedi `src/bot_config.example.json`).

Comandi utili:

```bash
sudo systemctl status tiktok-bot     # stato
sudo journalctl -u tiktok-bot -f     # log in tempo reale
sudo systemctl restart tiktok-bot    # riavvio manuale
```

Per aggiornare dopo un `git push`:

```bash
cd /opt/tiktok-recorder-bot
sudo bash deploy/setup.sh
```
