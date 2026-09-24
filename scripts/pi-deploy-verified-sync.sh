#!/usr/bin/env bash
# Run from a fresh checkout. Keeps the database, OAuth state and Funnel routes.
set -euo pipefail
umask 077
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$HOME/calorie-sync-backup-$(date +%Y%m%d-%H%M%S)"
sudo -v
OLD_DIR="$(sudo docker inspect calorie-bridge-app --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
PROJECT="$(sudo docker inspect calorie-bridge-app --format '{{index .Config.Labels "com.docker.compose.project"}}')"
test -n "$PROJECT"
test -f "$OLD_DIR/.env.pi"
mkdir -m 700 "$BACKUP_DIR"
sudo cp "$OLD_DIR/.env.pi" "$BACKUP_DIR/env.pi"
sudo chmod 600 "$BACKUP_DIR/env.pi"
if [ "$OLD_DIR" != "$SOURCE_DIR" ]; then
  sudo cp "$OLD_DIR/.env.pi" "$SOURCE_DIR/.env.pi"
  sudo chmod 600 "$SOURCE_DIR/.env.pi"
fi
# Keep running-code snapshots in case the Pi had local-only patches.
sudo docker cp calorie-bridge-app:/app/app "$BACKUP_DIR/app-before"
sudo docker cp calorie-work-mcp:/adapter/server.py "$BACKUP_DIR/mcp-server-before.py"
sudo docker exec calorie-bridge-db sh -c 'exec pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$BACKUP_DIR/database.sql"
test -s "$BACKUP_DIR/database.sql"
APP_IMAGE="$(sudo docker inspect calorie-bridge-app --format '{{.Image}}')"
MCP_IMAGE="$(sudo docker inspect calorie-work-mcp --format '{{.Image}}')"
sudo docker tag "$APP_IMAGE" calorie-bridge-app:before-verified-sync
sudo docker tag "$MCP_IMAGE" calorie-work-mcp:before-verified-sync
# Use the actual persistent mount paths, not a guessed installation folder.
MCP_CONFIG="$(sudo docker inspect calorie-work-mcp --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}')"
MCP_DATA="$(sudo docker inspect calorie-work-mcp --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')"
MCP_USER="$(sudo docker inspect calorie-work-mcp --format '{{.Config.User}}')"
test -n "$MCP_CONFIG" && test -n "$MCP_DATA" && test -n "$MCP_USER"
test -f "$MCP_CONFIG/config.json"
sudo cp -a "$MCP_CONFIG" "$BACKUP_DIR/mcp-config"

compose=(sudo docker compose --project-name "$PROJECT" --env-file "$SOURCE_DIR/.env.pi" -f "$SOURCE_DIR/docker-compose.pi.yml")
"${compose[@]}" build app
sudo docker build -t calorie-work-mcp:1.1.0 "$SOURCE_DIR/work_mcp"

run_adapter() {
  sudo docker run -d --name calorie-work-mcp --restart unless-stopped \
    --network host --user "$MCP_USER" --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --log-opt max-size=10m --log-opt max-file=3 \
    -v "$MCP_CONFIG:/config:ro" -v "$MCP_DATA:/data:rw" "$1"
}

"${compose[@]}" up -d --no-deps app
ready=0
for attempt in {1..30}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8021/health | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["api_version"]=="1.7.0" and x["fatsecret_sync_mode"]=="durable-outbox-readback"' 2>/dev/null; then ready=1; break; fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  printf 'services:\n  app:\n    image: calorie-bridge-app:before-verified-sync\n' > "$BACKUP_DIR/rollback.yml"
  "${compose[@]}" -f "$BACKUP_DIR/rollback.yml" up -d --no-build --no-deps app
  echo "App health check failed. Backup: $BACKUP_DIR; previous image: calorie-bridge-app:before-verified-sync" >&2
  exit 1
fi
sudo docker stop calorie-work-mcp
sudo docker rm calorie-work-mcp
if ! run_adapter calorie-work-mcp:1.1.0; then
  sudo docker rm -f calorie-work-mcp || true
  run_adapter calorie-work-mcp:before-verified-sync
  echo 'New adapter could not start; previous adapter restored.' >&2
  exit 1
fi
ready=0
for attempt in {1..30}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8023/health | python3 -c 'import json,sys; assert json.load(sys.stdin)["version"]=="1.1.0"' 2>/dev/null; then ready=1; break; fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  sudo docker stop calorie-work-mcp || true
  sudo docker rm calorie-work-mcp || true
  run_adapter calorie-work-mcp:before-verified-sync
  echo "New adapter failed health check; previous adapter restored. Backup: $BACKUP_DIR" >&2
  exit 1
fi
echo "Tracker 1.7.0 and MCP 1.1.0 are running. Backup: $BACKUP_DIR"
echo 'Next: log/check a meal and confirm FatSecret status is verified. Service health alone does not prove FatSecret delivery.'
echo 'Older unsynced meals need diary review before retrying; they are not blindly replayed.'
