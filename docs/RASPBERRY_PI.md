# Raspberry Pi deployment

This deployment removes the Render PostgreSQL dependency entirely. The Raspberry Pi runs both:

- `calorie-bridge-app` — the FastAPI dashboard/API container
- `calorie-bridge-db` — PostgreSQL 16 with a persistent Docker volume

Only the app is exposed publicly. PostgreSQL has **no host port** and is reachable only on the private Docker network.

The app binds to `127.0.0.1:8021` on the Pi. Tailscale Funnel publishes that local HTTP service on a stable public HTTPS `*.ts.net` URL, so ChatGPT Actions and FatSecret OAuth can reach it without router port-forwarding.

## Fresh install

Run these commands on the Raspberry Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/MushroomStew01/chatgpt-calorie-bridge/main/scripts/pi-setup.sh -o /tmp/pi-setup.sh
bash /tmp/pi-setup.sh
```

The script installs Docker and Tailscale if necessary, clones/updates the repository, creates `.env.pi`, starts PostgreSQL and the app, enables Tailscale Funnel, and updates `PUBLIC_BASE_URL` to the Pi's public HTTPS hostname.

During setup you will be asked for:

- Dashboard username (default `andy`)
- Existing dashboard password, or press Enter to generate a new one
- Existing `APP_API_KEY` from Render, or press Enter to generate a new one
- FatSecret OAuth 1.0 Consumer Key
- FatSecret OAuth 1.0 Consumer Secret

If you reuse the existing Render `APP_API_KEY`, the custom GPT's API-key authentication does not need to change. You will still need to change the Action server URL to the Pi's new `*.ts.net` URL.

FatSecret access tokens are intentionally not migrated. After the Pi is live, open the dashboard and click **Connect FatSecret** once to authorize the new database deployment.

## Validate

```bash
cd ~/chatgpt-calorie-bridge
bash scripts/pi-validate.sh
```

The validation script checks:

- Raspberry Pi OS/CPU architecture
- Docker and Docker Compose
- both container health states
- PostgreSQL readiness and table access
- local API health
- Tailscale and Funnel status
- public HTTPS API health
- API-key authenticated daily summary
- ChatGPT Action schema URL and operations
- recent app/database logs

It redacts secret values, so its output is safe to send back for troubleshooting.

## Useful commands

```bash
cd ~/chatgpt-calorie-bridge

# Status
sudo docker compose --env-file .env.pi -f docker-compose.pi.yml ps

# Logs
sudo docker compose --env-file .env.pi -f docker-compose.pi.yml logs -f app db

# Restart
sudo docker compose --env-file .env.pi -f docker-compose.pi.yml restart

# Update to latest GitHub code

git pull --ff-only origin main
sudo docker compose --env-file .env.pi -f docker-compose.pi.yml up -d --build

# Tailscale Funnel status
sudo tailscale funnel status

# Stop the public Funnel
sudo tailscale funnel --https=443 off

# Stop app/database without deleting data
sudo docker compose --env-file .env.pi -f docker-compose.pi.yml down

# Start again
sudo docker compose --env-file .env.pi -f docker-compose.pi.yml up -d
```

Do **not** run `docker compose down -v` unless you intentionally want to erase the PostgreSQL volume.

## Cutover from Render

Keep Render running until validation succeeds and the custom GPT has logged a real meal through the Pi URL. After that you can remove the Render web service/database. The Pi deployment starts with a fresh database, as requested; no Render meal history or FatSecret access token is copied.
