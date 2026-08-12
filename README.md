# ChatGPT Calorie Bridge

A FastAPI service that makes your photo-calorie workflow viewable outside ChatGPT.

## Features

- `POST /api/meals` stores calories and macros.
- `/` is a mobile-friendly daily dashboard.
- `GET /api/summary?day=YYYY-MM-DD` returns daily totals.
- SQLite is the default database.
- Optional FatSecret diary sync when a FatSecret `food_id` and `serving_id` are known.
- `openapi-chatgpt.yaml` for a ChatGPT action/tool.
- Docker, Render config, and GitHub Actions CI.

## Important FatSecret limitation

FatSecret `food_entry.create` requires a FatSecret food ID and serving ID. A photo-derived estimate such as “700 kcal, 35 g protein” cannot be posted as an arbitrary diary entry unless it is first mapped to a FatSecret food/serving (or an eligible custom-food API is available). This project therefore always stores the ChatGPT estimate in its own database and treats FatSecret mirroring as optional.

## Local run

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000/` for the dashboard and `http://localhost:8000/docs` for API docs.

## Deploy

Push to GitHub, then deploy the included `render.yaml`. Set `APP_API_KEY` and, optionally, the FatSecret OAuth 1.0 secrets as deployment environment variables.

## Connect ChatGPT

Replace the placeholder server URL in `openapi-chatgpt.yaml` with the deployed URL and configure API-key auth using `X-API-Key`.

Intended flow: `food photo → ChatGPT estimates nutrition → POST /api/meals → dashboard updates`.
