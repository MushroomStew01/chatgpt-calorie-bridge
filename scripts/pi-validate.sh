#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/chatgpt-calorie-bridge}"
COMPOSE_FILE="docker-compose.pi.yml"
ENV_FILE=".env.pi"

cd "$APP_DIR"
[ -f "$ENV_FILE" ] || { echo "Missing $APP_DIR/$ENV_FILE"; exit 1; }

get_env() {
  python3 - "$ENV_FILE" "$1" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
for line in path.read_text().splitlines():
    if line.startswith(key + "="):
        print(line.split("=", 1)[1])
        break
PY
}

PUBLIC_BASE_URL="$(get_env PUBLIC_BASE_URL)"
APP_API_KEY="$(get_env APP_API_KEY)"
HOST_PORT="$(get_env CALORIE_BRIDGE_HOST_PORT)"
HOST_PORT="${HOST_PORT:-8021}"

section() { printf '\n===== %s =====\n' "$*"; }

section "SYSTEM"
date -Is
uname -a
printf 'Architecture: '; uname -m
printf 'OS: '; grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- || true

section "DOCKER"
sudo docker --version
sudo docker compose version
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps

section "POSTGRESQL"
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  pg_isready -U calorie_bridge -d calorie_bridge
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  psql -U calorie_bridge -d calorie_bridge -v ON_ERROR_STOP=1 -c \
  "SELECT current_database() AS database, current_user AS db_user; SELECT COUNT(*) AS meal_rows FROM meals; SELECT COUNT(*) AS fatsecret_connection_rows FROM fatsecret_connection;"

section "LOCAL API"
curl -fsS "http://127.0.0.1:${HOST_PORT}/health" | jq .

section "TAILSCALE"
sudo tailscale status
sudo tailscale funnel status

section "PUBLIC API"
echo "Public URL: ${PUBLIC_BASE_URL}"
curl -fsS "${PUBLIC_BASE_URL}/health" | jq .

section "AUTHENTICATED SUMMARY"
curl -fsS -H "X-API-Key: ${APP_API_KEY}" "${PUBLIC_BASE_URL}/api/summary" | jq .

section "CHATGPT ACTION SCHEMA"
curl -fsS "${PUBLIC_BASE_URL}/action-openapi.json" | jq '{
  openapi,
  title: .info.title,
  version: .info.version,
  server: .servers[0].url,
  operations: [
    .paths["/api/meals"].post.operationId,
    .paths["/api/meals"].get.operationId,
    .paths["/api/summary"].get.operationId
  ],
  logMealConsequential: .paths["/api/meals"].post["x-openai-isConsequential"]
}'

section "RECENT APP LOGS"
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=40 app

section "RECENT DB LOGS"
sudo docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=30 db

section "SAFE CONFIG SUMMARY"
echo "PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"
echo "CALORIE_BRIDGE_HOST_PORT=${HOST_PORT}"
echo "APP_API_KEY=<redacted>"
echo "DASHBOARD_PASSWORD=<redacted>"
echo "POSTGRES_PASSWORD=<redacted>"
echo "FATSECRET_CONSUMER_SECRET=<redacted>"

echo
echo "VALIDATION COMPLETE"
echo "Send the full output of this script back for review. It intentionally does not print secret values."
