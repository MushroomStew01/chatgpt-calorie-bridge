#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${CALORIE_MCP_DIR:-$HOME/calorie-work-mcp}"
[ -f "$INSTALL_DIR/config/config.json" ] || { echo 'Existing MCP config not found'; exit 1; }
sudo docker inspect calorie-work-mcp >/dev/null
sudo docker build -t calorie-work-mcp:1.0.2 "$SOURCE_DIR/work_mcp"
# Only replace the adapter; its persistent config/state and existing Funnel stay.
sudo docker stop calorie-work-mcp
sudo docker rm calorie-work-mcp
sudo docker run -d --name calorie-work-mcp --restart unless-stopped \
  --network host --user "$(id -u):$(id -g)" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v "$INSTALL_DIR/config:/config:ro" -v "$INSTALL_DIR/data:/data:rw" \
  calorie-work-mcp:1.0.2
for attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8023/health; then
    echo
    curl -fsS --max-time 5 http://127.0.0.1:8023/.well-known/oauth-authorization-server/
    echo
    exit 0
  fi
  sleep 2
done
echo 'Adapter failed to start; inspect: sudo docker logs --tail 40 calorie-work-mcp' >&2
exit 1
