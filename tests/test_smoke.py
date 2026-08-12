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
from app.main import app

client = TestClient(app)
API_HEADERS = {"X-API-Key": "test-key"}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


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


def test_dashboard_requires_basic_auth():
    assert client.get("/").status_code == 401
    response = client.get("/", auth=("test-user", "test-pass"))
    assert response.status_code == 200
    assert "Calorie Dashboard" in response.text


def test_dynamic_action_schema():
    response = client.get("/action-openapi.json")
    assert response.status_code == 200
    body = response.json()
    assert body["paths"]["/api/meals"]["post"]["operationId"] == "logMeal"
    assert body["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "X-API-Key"
