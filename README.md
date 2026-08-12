# ChatGPT Calorie Bridge

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FMushroomStew01%2Fchatgpt-calorie-bridge)

A small FastAPI service for the workflow:

`food photo → ChatGPT estimates nutrition → API logs meal → private web dashboard updates`

## What it includes

- `POST /api/meals` to store calories, protein, carbs, fat, fiber, and sugar.
- `GET /api/meals` and `GET /api/summary` for meal history and daily totals.
- A password-protected mobile-friendly dashboard at `/`.
- A configurable daily calorie goal (default: 2,000 kcal) with calories remaining.
- Local-day handling using `America/Toronto` by default so late-night entries do not fall onto the wrong UTC date.
- SQLite for local development.
- PostgreSQL support for hosted persistence.
- A Render Blueprint (`render.yaml`) that creates the web service and PostgreSQL database together.
- API-key protection for all meal read/write endpoints.
- A dynamic ChatGPT Action schema at `/action-openapi.json`.
- Optional FatSecret diary mirroring when a valid FatSecret `food_id` and `serving_id` are supplied.
- GitHub Actions CI.

## Important FatSecret limitation

FatSecret `food_entry.create` requires a FatSecret food ID and serving ID. A photo-derived estimate such as “700 kcal, 35 g protein” cannot be posted as an arbitrary diary entry unless it is first mapped to a FatSecret food/serving. This project therefore always stores the ChatGPT estimate in its own database and treats FatSecret mirroring as optional.

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
```

Then open `http://localhost:8000/` and sign in with the dashboard username/password.

API docs are available at `http://localhost:8000/docs`.

## Deploy on Render

Click the **Deploy to Render** button at the top of this README. The included Blueprint creates:

- a Docker web service;
- a PostgreSQL database;
- a generated `APP_API_KEY`;
- a generated `DASHBOARD_PASSWORD`;
- the Toronto timezone and 2,000 kcal daily goal.

After deployment, open the web service's Environment page and copy the generated values for `APP_API_KEY` and `DASHBOARD_PASSWORD` somewhere secure. The dashboard username is `andy` unless you change it.

### Render free-tier note

The Blueprint currently uses Render's free web and free PostgreSQL plans for easy testing. Render free PostgreSQL databases expire after 30 days, so upgrade the database before expiry if you want the calorie history to remain there long-term. Do not switch the hosted deployment back to SQLite: Render web-service files are ephemeral and a SQLite calorie database can be lost on restart/redeploy.

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

## API examples

Log a meal:

```bash
curl -X POST "http://localhost:8000/api/meals" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace-with-your-key" \
  -d '{
    "name": "Beef macaroni bowl",
    "calories": 700,
    "protein": 35,
    "carbs": 72,
    "fat": 30,
    "fiber": 9,
    "sugar": 10,
    "meal_type": "dinner"
  }'
```

Get today's summary:

```bash
curl "http://localhost:8000/api/summary" \
  -H "X-API-Key: replace-with-your-key"
```

## FatSecret integration

The bridge database is the source of truth. FatSecret mirroring is optional.

FatSecret's food-diary create call requires an existing FatSecret `food_id` and `serving_id`; it does not accept an arbitrary photo-derived calorie estimate by itself. If those IDs are supplied with a meal and the four FatSecret OAuth 1.0 environment variables are configured, the bridge attempts to mirror the entry to FatSecret.

```env
FATSECRET_CONSUMER_KEY=
FATSECRET_CONSUMER_SECRET=
FATSECRET_ACCESS_TOKEN=
FATSECRET_ACCESS_TOKEN_SECRET=
```

The bridge will still save the meal locally if FatSecret syncing is not configured or fails.

## Security

- Meal API reads and writes require `X-API-Key`.
- The dashboard uses HTTP Basic authentication.
- Secrets belong in environment variables, never in the repository.
- `.env`, SQLite databases, virtual environments, and Python caches are ignored by Git.
