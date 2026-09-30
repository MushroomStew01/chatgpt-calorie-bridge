#!/usr/bin/env bash
# Additive installation. Does not rebuild or restart the existing app/database.
set -euo pipefail
umask 077
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${CALORIE_MCP_DIR:-$HOME/calorie-work-mcp}"
command -v python3 >/dev/null
sudo docker inspect calorie-bridge-app >/dev/null
sudo tailscale status >/dev/null
mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data"
chmod 700 "$INSTALL_DIR" "$INSTALL_DIR/config" "$INSTALL_DIR/data"

# Save a reviewable pre-install routing snapshot; never reset existing Funnel routes.
sudo tailscale serve status --json > "$INSTALL_DIR/tailscale-before.json"
python3 - "$INSTALL_DIR/tailscale-before.json" <<'PY'
import json, sys
config=json.load(open(sys.argv[1]))
if '8443' in config.get('TCP',{}):
    raise SystemExit('Port 8443 already has a Tailscale service. Stop here; do not replace it.')
import socket
with socket.socket() as sock:
    try: sock.bind(('127.0.0.1',8023))
    except OSError: raise SystemExit('Local port 8023 is occupied. Stop here; do not replace it.')
PY

DNS_NAME="$(sudo tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
export CALORIE_MCP_PUBLIC_URL="https://${DNS_NAME}:8443"
export CALORIE_MCP_CONFIG_PATH="$INSTALL_DIR/config/config.json"
# Secrets go directly from the running container into a mode-600 local config file.
sudo docker inspect calorie-bridge-app | python3 -c '
import json,os,pathlib,sys,urllib.request
env=dict(value.split("=",1) for value in json.load(sys.stdin)[0]["Config"]["Env"])
key=env.get("APP_API_KEY","")
if not key or key=="change-me": sys.exit("A real APP_API_KEY is required")
if not env.get("DASHBOARD_PASSWORD"): sys.exit("Configure the existing dashboard password first")
request=urllib.request.Request("http://127.0.0.1:8021/api/summary",headers={"X-API-Key":key})
with urllib.request.urlopen(request,timeout=15) as response: response.read()
p=pathlib.Path(os.environ["CALORIE_MCP_CONFIG_PATH"])
p.write_text(json.dumps({"public_url":os.environ["CALORIE_MCP_PUBLIC_URL"],"api_key":key,"bridge_url":"http://127.0.0.1:8021"}))
p.chmod(0o600)
'

sudo docker build -t calorie-work-mcp:1.1.0 "$SOURCE_DIR/work_mcp"
sudo docker run -d --name calorie-work-mcp --restart unless-stopped \
  --network host --user "$(id -u):$(id -g)" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v "$INSTALL_DIR/config:/config:ro" -v "$INSTALL_DIR/data:/data:rw" \
  calorie-work-mcp:1.1.0

ready=0
for attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8023/health >/dev/null; then ready=1; break; fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  echo 'MCP container did not start. Existing calorie bridge is unaffected.' >&2
  sudo docker logs --tail 40 calorie-work-mcp
  exit 1
fi
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8023
curl -fsS --retry 3 --max-time 20 "$CALORIE_MCP_PUBLIC_URL/health"
echo
curl -fsS --max-time 20 "$CALORIE_MCP_PUBLIC_URL/.well-known/oauth-protected-resource/mcp"
echo
printf '\nMCP endpoint: %s/mcp\nSign in using your existing calorie dashboard username and password.\n' "$CALORIE_MCP_PUBLIC_URL"
