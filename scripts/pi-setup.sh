#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/MushroomStew01/chatgpt-calorie-bridge.git"
APP_DIR="${APP_DIR:-$HOME/chatgpt-calorie-bridge}"
HOST_PORT="${CALORIE_BRIDGE_HOST_PORT:-8021}"
COMPOSE_FILE="docker-compose.pi.yml"
ENV_FILE=".env.pi"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

say "Installing required host packages"
sudo apt-get update
sudo apt-get install -y ca-certificates curl git jq openssl

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker Engine"
  curl -fsSL https://get.docker.com | sudo sh
fi

sudo systemctl enable --now docker

if ! sudo docker compose version >/dev/null 2>&1; then
  die "Docker Compose v2 is not available. Run: sudo apt-get install -y docker-compose-plugin"
fi

if ! command -v tailscale >/dev/null 2>&1; then
  say "Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

sudo systemctl enable --now tailscaled

if ! sudo tailscale status >/dev/null 2>&1; then
  say "Connecting this Raspberry Pi to Tailscale"
  echo "A login/authorization URL may be shown below. Complete it, then return here."
  sudo tailscale up
fi

say "Cloning/updating calorie bridge"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin main
  git -C "$APP_DIR" reset --hard origin/main
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "${ENV_FILE}.backup.$(date +%Y%m%d-%H%M%S)"
  echo "Existing $ENV_FILE backed up."
fi

read -r -p "Dashboard username [andy]: " DASHBOARD_USERNAME
DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-andy}"

read -r -s -p "Existing Dashboard password (Enter to generate a new one): " DASHBOARD_PASSWORD
echo
if [ -z "$DASHBOARD_PASSWORD" ]; then
  DASHBOARD_PASSWORD="$(openssl rand -hex 24)"
fi

read -r -s -p "Existing APP_API_KEY from Render (Enter to generate a new one): " APP_API_KEY
echo
if [ -z "$APP_API_KEY" ]; then
  APP_API_KEY="$(openssl rand -hex 32)"
fi

read -r -p "FatSecret Consumer Key: " FATSECRET_CONSUMER_KEY
[ -n "$FATSECRET_CONSUMER_KEY" ] || die "FatSecret Consumer Key cannot be blank."

read -r -s -p "FatSecret Consumer Secret: " FATSECRET_CONSUMER_SECRET
echo
[ -n "$FATSECRET_CONSUMER_SECRET" ] || die "FatSecret Consumer Secret cannot be blank."

POSTGRES_PASSWORD="$(openssl rand -hex 32)"

cat > "$ENV_FILE" <<EOF
POSTGRES_DB=calorie_bridge
POSTGRES_USER=calorie_bridge
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
CALORIE_BRIDGE_HOST_PORT=${HOST_PORT}
APP_API_KEY=${APP_API_KEY}
DASHBOARD_USERNAME=${DASHBOARD_USERNAME}
DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}
APP_TIMEZONE=America/Toronto
DAILY_CALORIE_GOAL=2000
PUBLIC_BASE_URL=http://127.0.0.1:${HOST_PORT}
FATSECRET_CONSUMER_KEY=${FATSECRET_CONSUMER_KEY}
FATSECRET_CONSUMER_SECRET=${FATSECRET_CONSUMER_SECRET}
FATSECRET_ACCESS_TOKEN=
FATSECRET_ACCESS_TOKEN_SECRET=
FATSECRET_MATCH_MIN_SCORE=0.32
FATSECRET_MAX_SEARCH_RESULTS=12
FATSECRET_DETAIL_CANDIDATES=6
EOF
chmod 600 "$ENV_FILE"

say "Building and starting PostgreSQL + calorie bridge"
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build

say "Waiting for local API health check"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
    sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=100 app db
    die "Local API did not become healthy."
  fi
  sleep 2
done

say "Publishing only the web API/dashboard through Tailscale Funnel"
echo "If Tailscale asks you to enable Funnel in a browser, approve it."
sudo tailscale funnel --bg "$HOST_PORT"

DNS_NAME="$(sudo tailscale status --json | jq -r '.Self.DNSName // empty' | sed 's/\.$//')"
[ -n "$DNS_NAME" ] || die "Could not determine the Tailscale DNS name."
PUBLIC_BASE_URL="https://${DNS_NAME}"

python3 - "$ENV_FILE" "$PUBLIC_BASE_URL" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
url = sys.argv[2]
lines = path.read_text().splitlines()
out = []
found = False
for line in lines:
    if line.startswith("PUBLIC_BASE_URL="):
        out.append(f"PUBLIC_BASE_URL={url}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"PUBLIC_BASE_URL={url}")
path.write_text("\n".join(out) + "\n")
PY

say "Restarting app with the public HTTPS URL"
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --force-recreate app

for i in $(seq 1 20); do
  if curl -fsS "${PUBLIC_BASE_URL}/health" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 20 ]; then
    echo "Public health check did not succeed yet. Check Funnel status with:"
    echo "  sudo tailscale funnel status"
    break
  fi
  sleep 2
done

say "Raspberry Pi calorie bridge is configured"
echo "Public URL: ${PUBLIC_BASE_URL}"
echo "Dashboard: ${PUBLIC_BASE_URL}/"
echo "Action schema: ${PUBLIC_BASE_URL}/action-openapi.json"
echo
echo "IMPORTANT:"
echo "1. Open the dashboard and click Connect FatSecret again. Tokens were intentionally not migrated."
echo "2. In the custom GPT Action, import: ${PUBLIC_BASE_URL}/action-openapi.json"
echo "3. If you generated a new APP_API_KEY, update the GPT Action authentication key."
echo "4. To display the API key locally, run: grep '^APP_API_KEY=' '${APP_DIR}/${ENV_FILE}'"
echo "5. Run validation next: cd '${APP_DIR}' && bash scripts/pi-validate.sh"
