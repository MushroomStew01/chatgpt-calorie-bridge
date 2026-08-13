# ChatGPT Calorie Bridge

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FMushroomStew01%2Fchatgpt-calorie-bridge)

A small FastAPI service for the workflow:

`food photo → ChatGPT estimates nutrition → API logs meal → FatSecret match/sync → private web dashboard updates`

## What it includes

- `POST /api/meals` to store calories, protein, carbs, fat, fiber, and sugar.
- `GET /api/meals` and `GET /api/summary` for meal history and daily totals.
- Automatic FatSecret food search, serving selection, portion scaling, and diary sync when FatSecret is connected.
- A password-protected mobile-friendly dashboard at `/`, including per-meal FatSecret sync status.
- A configurable daily calorie goal (default: 2,000 kcal) with calories remaining.
- Local-day handling using `America/Toronto` by default so late-night entries do not fall onto the wrong UTC date.
- SQLite for local development and PostgreSQL support for hosted persistence.
- A Render Blueprint (`render.yaml`) that creates the web service and PostgreSQL database together.
- API-key protection for all meal read/write endpoints.
- A dynamic ChatGPT Action schema at `/action-openapi.json`.
- GitHub Actions CI.

## How automatic FatSecret matching works

The bridge always stores the ChatGPT photo estimate in its own database first. If a FatSecret account is connected, the bridge then:

1. Searches FatSecret using the meal name (or optional `fatsecret_search_query`).
2. Ranks matching foods by name similarity.
3. Retrieves available servings for the strongest candidates.
4. Compares calories, protein, carbs, and fat against the ChatGPT estimate.
5. Prefers a usable FatSecret serving and ignores derived `serving_id=0` servings that cannot be used in `food_entry.create`.
6. For generic foods, scales `number_of_units` so the FatSecret diary calories track the photo estimate as closely as possible.
7. Creates the FatSecret diary entry only when the match clears the configured confidence threshold.

The local calorie estimate remains the source of truth. If FatSecret cannot find a reasonable match, the meal remains safely logged in the bridge as `Local only` rather than silently writing an unrelated food.

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

Recommended custom-GPT behavior: when analyzing a food photo, provide a concise FatSecret search phrase in `fatsecret_search_query` when useful. For example, the visible meal name might be `Beef macaroni bowl with peas and carrots`, while the search query could be `macaroni with beef`.

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

When FatSecret is connected, the API will attempt the search/match/sync automatically. The returned meal includes `fatsecret_entry_id` when the FatSecret diary entry was successfully created.

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
