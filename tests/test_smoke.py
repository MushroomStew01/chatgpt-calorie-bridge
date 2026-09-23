import os
from pathlib import Path

TEST_DB = Path("test_calories.db")
if TEST_DB.exists():
    TEST_DB.unlink()

os.environ["APP_API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = "sqlite:///./test_calories.db"
os.environ["DASHBOARD_USERNAME"] = "test-user"
os.environ["DASHBOARD_PASSWORD"] = "test-pass"
os.environ["APP_TIMEZONE"] = "America/Toronto"
os.environ["DAILY_CALORIE_GOAL"] = "2000"

from fastapi.testclient import TestClient
from app import fatsecret
from app.main import app

client = TestClient(app)
API_HEADERS = {"X-API-Key": "test-key"}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["api_version"] == "1.6.0"
    assert body["fatsecret_keys_configured"] is False
    assert body["fatsecret_connected"] is False
    assert body["fatsecret_oauth_signer"] == "manual-rfc3986-hmac-sha1"
    assert body["fatsecret_auto_match"] is False
    assert body["fatsecret_exact_calories"] is True
    assert body["fatsecret_sync_mode"] == "background"
    assert body["get_meals_default_scope"] == "today"


def test_api_reads_require_key():
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/meals").status_code == 401


def test_log_and_summarize_meal():
    response = client.post(
        "/api/meals",
        headers=API_HEADERS,
        json={
            "name": "Test meal",
            "calories": 500,
            "protein": 30,
            "carbs": 50,
            "fat": 20,
        },
    )
    assert response.status_code == 200
    assert response.json()["calories"] == 500

    summary = client.get("/api/summary", headers=API_HEADERS)
    assert summary.status_code == 200
    body = summary.json()
    assert body["calories"] == 500
    assert body["calorie_goal"] == 2000
    assert body["calories_remaining"] == 1500
    assert body["timezone"] == "America/Toronto"


def test_get_meals_defaults_to_today_instead_of_lifetime_history():
    old = client.post(
        "/api/meals",
        headers=API_HEADERS,
        json={
            "name": "Very old meal",
            "calories": 123,
            "eaten_at": "2020-01-01T12:00:00Z",
        },
    )
    assert old.status_code == 200

    response = client.get("/api/meals", headers=API_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body) <= 25
    assert all(meal["name"] != "Very old meal" for meal in body)


def test_dashboard_requires_basic_auth():
    assert client.get("/").status_code == 401
    response = client.get("/", auth=("test-user", "test-pass"))
    assert response.status_code == 200
    assert "Calorie Dashboard" in response.text
    assert "FatSecret keys not configured" in response.text


def test_fatsecret_connect_requires_dashboard_auth_and_keys():
    assert client.get("/fatsecret/connect").status_code == 401
    response = client.get("/fatsecret/connect", auth=("test-user", "test-pass"))
    assert response.status_code == 503


def test_oauth_signer_matches_rfc5849_example():
    oauth = fatsecret.oauth_parameters(
        consumer_key="dpf43f3p2l4k3l03",
        consumer_secret="kd94hf93k423kf44",
        method="GET",
        url="http://photos.example.net/photos",
        request_parameters={"file": "vacation.jpg", "size": "original"},
        token="nnch734d00sl2jdk",
        token_secret="pfkkdhi9sl3r4s00",
        timestamp=1191242096,
        nonce="kllo9940pd9333jh",
    )
    assert oauth["oauth_signature"] == "tR3+Ty81lMeYAr/Fid0kMTYa/WM="


def test_dynamic_action_schema():
    response = client.get("/action-openapi.json")
    assert response.status_code == 200
    body = response.json()
    action = body["paths"]["/api/meals"]["post"]
    assert action["operationId"] == "logMeal"
    assert action["x-openai-isConsequential"] is False
    props = action["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "fatsecret_search_query" in props
    assert body["components"]["schemas"] == {}
    assert body["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "X-API-Key"


def test_background_sync_uses_exact_food_even_with_catalog_ids(monkeypatch):
    from unittest.mock import Mock
    from app import main

    monkeypatch.setattr(main, "fatsecret_connected", lambda db: True)
    monkeypatch.setattr(main, "fatsecret_access_credentials", lambda db: ("token", "secret"))
    search = Mock(side_effect=AssertionError("Exact sync must not search"))
    custom = Mock(return_value=("900", "901"))
    diary = Mock(return_value="902")
    monkeypatch.setattr(fatsecret, "find_best_food_match", search)
    monkeypatch.setattr(fatsecret, "create_exact_food", custom)
    monkeypatch.setattr(fatsecret, "create_diary_entry", diary)
    result = client.post("/api/meals", headers=API_HEADERS, json={
        "name": "Pizza - 3 slices", "calories": 540, "protein": 24,
        "carbs": 66, "fat": 20, "fiber": 4, "sugar": 6,
        "eaten_at": "2026-09-23T00:30:00Z",
        "fatsecret_food_id": "old", "fatsecret_serving_id": "old-serving",
        "fatsecret_number_of_units": 99,
    })
    assert result.status_code == 200
    meal_id = result.json()["id"]
    with main.SessionLocal() as db:
        meal = db.get(main.Meal, meal_id)
        assert meal.calories == 540
        assert meal.fatsecret_entry_id == "902"
        assert "exact 540 kcal" in meal.notes
    assert custom.call_args.kwargs["calories"] == 540
    assert diary.call_args.kwargs["expected_calories"] == 540
    assert diary.call_args.kwargs["number_of_units"] == 1
    assert diary.call_args.kwargs["food_id"] == "900"
    from datetime import date
    assert diary.call_args.kwargs["date_int"] == (date(2026, 9, 22) - date(1970, 1, 1)).days
    main.run_fatsecret_sync(meal_id, None, 1)
    assert diary.call_count == 1
    search.assert_not_called()


def test_background_failure_preserves_local_meal_and_visible_error(monkeypatch):
    from unittest.mock import Mock
    from app import main

    monkeypatch.setattr(main, "fatsecret_connected", lambda db: True)
    monkeypatch.setattr(main, "fatsecret_access_credentials", lambda db: ("token", "secret"))
    monkeypatch.setattr(fatsecret, "create_exact_food", Mock(
        side_effect=fatsecret.FatSecretError("API error 13: Permission denied")))
    result = client.post("/api/meals", headers=API_HEADERS, json={
        "name": "Pizza", "calories": 540, "notes": "x" * 500,
    })
    assert result.status_code == 200
    with main.SessionLocal() as db:
        meal = db.get(main.Meal, result.json()["id"])
        assert meal.calories == 540
        assert meal.fatsecret_entry_id is None
        assert "FatSecret sync failed: API error 13: Permission denied" in meal.notes
        assert len(meal.notes) <= 500
