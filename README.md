# ChatGPT Calorie Bridge

For the Raspberry Pi phone-photo workflow, see [Mobile photo logging](docs/MOBILE_PHOTO_LOGGING.md). Check the existing-GPT prerequisite first; a connected desktop-only plugin does not establish mobile support.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FMushroomStew01%2Fchatgpt-calorie-bridge)

A small FastAPI service for the workflow:

`food photo → ChatGPT estimates nutrition → API logs meal → FatSecret exact-calorie sync → private web dashboard updates`

## What it includes

- `POST /api/meals` to store calories, protein, carbs, fat, fiber, and sugar.
- `GET /api/meals` and `GET /api/summary` for meal history and daily totals.
- Automatic FatSecret custom-food creation, calorie-matched catalog fallback, and verified diary sync.
- A password-protected mobile-friendly dashboard at `/`, including per-meal FatSecret sync status.
- A configurable daily calorie goal (default: 2,000 kcal) with calories remaining.
- Local-day handling using `America/Toronto` by default so late-night entries do not fall onto the wrong UTC date.
- SQLite for local development and PostgreSQL support for hosted persistence.
- A Render Blueprint (`render.yaml`) that creates the web service and PostgreSQL database together.
- API-key protection for all meal read/write endpoints.
- A dynamic ChatGPT Action schema at `/action-openapi.json`.
- GitHub Actions CI.

## How verified FatSecret sync works

The Pi saves the meal and a durable sync job together. A background worker creates
a custom food with the supplied nutrition (or a matching catalog food scaled to
the supplied calories when custom creation is unavailable), posts one diary entry, and independently
reads it back to check calories, date and identity. Only then is it marked verified.

The queue survives restarts. Transient failures retry with backoff. Uncertain diary
writes are reconciled using a stable marker, never blindly reposted. Missing API
permissions and mismatched entries remain visible as blocked or needing review.
Older untracked meals are not automatically replayed.

After every log, call `getMealSyncStatus` and report **Pi saved** and **FatSecret
verified/pending/failed** separately. A queued response or entry ID alone is not
proof of sync. The MCP adapter performs an initial check automatically; it does
not turn a verification failure into a failed local save.

Deploy the tracker and MCP adapter together with
`bash scripts/pi-deploy-verified-sync.sh` from a fresh checkout on the Pi.
See [verification, recovery and deployment details](docs/VERIFIED_SYNC.md).

FatSecret must be connected. If custom creation is unavailable, catalog fallback
preserves logged calories; catalog macros can differ from the Pi's estimates.
Outages remain queued, and rounding mismatches require review rather than claiming success.

## Local run

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

python -m pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Update `.env` before using the app:

```env
APP_API_KEY=replace-with-a-long-random-secret
DASHBOARD_USERNAME=andy
DASHBOARD_PASSWORD=replace-with-another-long-random-secret
APP_TIMEZONE=America/Toronto
DAILY_CALORIE_GOAL=2000
DATABASE_URL=sqlite:///./calories.db
FATSECRET_CONSUMER_KEY=
FATSECRET_CONSUMER_SECRET=

# Optional tuning
FATSECRET_MATCH_MIN_SCORE=0.32
FATSECRET_MAX_SEARCH_RESULTS=12
FATSECRET_DETAIL_CANDIDATES=6
```

Then open `http://localhost:8000/` and sign in with the dashboard username/password.

API docs are available at `http://localhost:8000/docs`.

## Deploy on Render

Click the **Deploy to Render** button at the top of this README. The included Blueprint creates a Docker web service, PostgreSQL database, generated `APP_API_KEY`, generated `DASHBOARD_PASSWORD`, Toronto timezone, and 2,000 kcal daily goal.

After deployment, add your FatSecret OAuth 1.0 Consumer Key and Consumer Secret to the Render Environment page:

```env
FATSECRET_CONSUMER_KEY=...
FATSECRET_CONSUMER_SECRET=...
PUBLIC_BASE_URL=https://YOUR-SERVICE.onrender.com
```

Do not put FatSecret secrets in GitHub.

Open the dashboard and click **Connect FatSecret**. The OAuth 1.0 request token and permanent user access token are handled by the bridge; the permanent access token/secret are persisted in PostgreSQL. You do not need to manually create `FATSECRET_ACCESS_TOKEN` environment variables for a normal connection.

### Render free-tier note

The Blueprint currently uses Render's free web and free PostgreSQL plans for easy testing. Free database availability/retention can change, so use a persistent paid database before relying on the service for long-term history. Do not switch the hosted deployment back to SQLite: Render web-service files are ephemeral and a SQLite calorie database can be lost on restart/redeploy.

## Connect it to ChatGPT

The easiest personal setup is a custom GPT with an Action.

1. Deploy this project and copy your Render service URL, for example `https://chatgpt-calorie-bridge.onrender.com`.
2. In ChatGPT, create/edit a GPT and open **Actions** → **Create new action**.
3. Import this schema URL:

   `https://YOUR-SERVICE.onrender.com/action-openapi.json`

4. Set authentication to **API key** using a **custom header**.
5. Header name: `X-API-Key`.
6. Secret value: the generated Render `APP_API_KEY`.
7. Test `getDailySummary` and `logMeal` in the GPT preview.

The repository also contains `openapi-chatgpt.yaml` as a static fallback schema, but the dynamic `/action-openapi.json` endpoint automatically uses the correct deployed domain.

A custom GPT Action is separate from the default ChatGPT conversation. Use the calorie-tracking custom GPT when you want `"log this"` to call this API automatically.

Recommended custom-GPT behavior: honor user-supplied calories and portions. Report local logging and confirmed FatSecret sync separately. Search hints are optional legacy inputs.

## API example

```bash
curl -X POST "http://localhost:8000/api/meals" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace-with-your-key" \
  -d '{
    "name": "Beef macaroni bowl with peas and carrots",
    "calories": 700,
    "protein": 35,
    "carbs": 72,
    "fat": 30,
    "fiber": 9,
    "sugar": 10,
    "meal_type": "dinner",
    "fatsecret_search_query": "macaroni with beef"
  }'
```

When FatSecret is connected, the API queues exact-calorie sync automatically. Call `getMealSyncStatus` afterward: only `fatsecret.status=verified` confirms the diary entry and its calories. An entry ID alone is not confirmation.

Get today's summary:

```bash
curl "http://localhost:8000/api/summary" \
  -H "X-API-Key: replace-with-your-key"
```

## Security

- Meal API reads and writes require `X-API-Key`.
- The dashboard uses HTTP Basic authentication.
- FatSecret Consumer and access secrets are never returned by the API.
- Secrets belong in environment variables or the private PostgreSQL connection record, never in the repository.
- `.env`, SQLite databases, virtual environments, and Python caches are ignored by Git.

## ChatGPT Work plugin

For an additive MCP/OAuth adapter on the existing Raspberry Pi, see
[ChatGPT Work setup](docs/CHATGPT_WORK.md). It keeps the existing REST API,
dashboard, database, and FatSecret workflow in place.
