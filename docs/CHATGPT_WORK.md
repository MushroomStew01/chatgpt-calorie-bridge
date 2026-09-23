# Calorie Logger in ChatGPT Work

The existing app is a REST API for GPT Actions. This upgrade adds an independent
MCP server for ChatGPT Work. It forwards calls to the existing API, retaining the
same meals, Toronto date rules, and asynchronous FatSecret sync.

## Architecture

- Existing dashboard/API: HTTPS 443 → 127.0.0.1:8021 (unchanged).
- Work MCP: HTTPS 8443 → 127.0.0.1:8023, container `calorie-work-mcp`.
- Sign-in uses the existing dashboard username/password, checked against the
  existing dashboard over localhost. Credentials are not stored in OAuth state.
- OAuth authorization code + S256 PKCE with public-client dynamic registration.
  Only ChatGPT connector callback URLs are accepted. Tokens are scoped to this
  MCP resource, expire, rotate on refresh, and support revocation.
- OAuth state and write deduplication live in `~/calorie-work-mcp/data`.
  API key lives in a mode-600 JSON file in `~/calorie-work-mcp/config`, copied
  directly from the running app. Never commit or upload these directories.
- No third-party identity-provider account or additional paid service is required.

This is a private single-owner adapter. It is not a multi-user OAuth service.
Use a maintained external identity provider before offering it to other users.
The server uses the SDK's authorization/PKCE handlers; the small provider stores
opaque tokens and checks your existing dashboard login.

## Install on the existing Raspberry Pi

The existing `calorie-bridge-app` must be running on localhost port 8021.
Docker, Tailscale Funnel and the dashboard password must already be configured.
Ports 8023 and 8443 must be free. The installer stops if either is occupied.

From a separate checkout of this branch:

```bash
git clone --branch work-plugin-mcp --single-branch \
  https://github.com/MushroomStew01/chatgpt-calorie-bridge.git \
  "$HOME/calorie-work-mcp-source"
bash "$HOME/calorie-work-mcp-source/scripts/pi-install-work-mcp.sh"
```

Do not rerun the older `pi-setup.sh`. The new installer builds only the new
container. It does not recreate the database or change the existing app, keys,
FatSecret connection, or port-443 Funnel route. It saves the pre-install
Tailscale configuration for reference. It deliberately refuses to overwrite
an existing installation; use the rollback commands before a clean reinstall.

The installer performs local and public health checks, prints the protected
resource metadata and the MCP endpoint. It does not create a test meal.
If the final public check fails, inspect `sudo tailscale funnel status` and
`sudo docker logs --tail 40 calorie-work-mcp`; retain your existing working app.

## Connect the plugin

For this Pi, the endpoint is:

`https://stewytailscale.tail93bb57.ts.net:8443/mcp`

The plugin's `mcp.json` points to that endpoint. Complete the ChatGPT connection
prompt with your existing calorie dashboard username and password. If client
registration mode is requested, select dynamic client registration (DCR), with
public-client (`none`) token authentication. Do not paste your APP_API_KEY into
ChatGPT or the plugin bundle.

Start a new Work conversation after the plugin update. Ask for today's calorie
total first. That verifies read access without adding a meal. Then use the usual
"log this" workflow.

## Tools

| Tool | Behavior |
| --- | --- |
| `getDailySummary` | Actual daily totals; omitted date means today in Toronto |
| `getMeals` | Up to 50 meals for a Toronto day, newest first; flags possible truncation |
| `logMeal` | Saves one meal through the existing API and queues its existing FatSecret sync |

`logMeal` requires a unique request_id for each intended meal. Repeating the same
ID and details returns the saved response, without another POST. Different
payloads using the same ID fail. The adapter never automatically retries a POST.
After a timeout or restart during a write, the ID remains marked uncertain:
inspect meals before deliberately issuing a new write. Records are retained
indefinitely; do not delete the data directory during upgrades.

## Rollback

These commands remove only the new endpoint/container; keep the data/config
folders so OAuth state and deduplication records are preserved.

```bash
sudo tailscale funnel --https=8443 off
sudo docker stop calorie-work-mcp
sudo docker rm calorie-work-mcp
```

The original GPT Actions and dashboard on port 443 continue to work.
If you later rotate APP_API_KEY, update the adapter config locally and restart
its container. Never share the config file. Changing the dashboard password
changes new sign-ins; already-issued OAuth sessions must be revoked separately.

## Development and verification

Adapter-only tests require `work_mcp/requirements.txt` and pytest. The integration
test and existing tests also require root `requirements.txt`; install
`sse-starlette==3.0.3` in that combined test environment because the existing
FastAPI version pins Starlette below the current sse-starlette minimum.
The production adapter is separately built and does not alter the app's pins.

```bash
python -m pytest -q
bash -n scripts/pi-install-work-mcp.sh
```

Tests cover discovery, rejected anonymous requests, restricted callbacks, consent
CSRF/origin checks, wrong credentials, PKCE failures, single-use codes, resource
binding, refresh/revocation, state persistence, duplicate/uncertain writes,
validation, and the real REST app's database/date/background-sync behavior.
Live Raspberry Pi Docker/Funnel and ChatGPT linking still require installation
and an actual sign-in; local tests do not establish that those steps succeeded.

References:
- https://developers.openai.com/plugins/build/auth
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0
- https://tailscale.com/docs/reference/tailscale-cli/funnel
