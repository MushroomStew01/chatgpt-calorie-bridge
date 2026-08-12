import os
os.environ["APP_API_KEY"]="test-key"
os.environ["DATABASE_URL"]="sqlite:///./test_calories.db"
from fastapi.testclient import TestClient
from app.main import app
client=TestClient(app)
def test_health():
    r=client.get("/health"); assert r.status_code==200; assert r.json()["status"]=="ok"
def test_log_meal():
    r=client.post("/api/meals",headers={"X-API-Key":"test-key"},json={"name":"Test meal","calories":500,"protein":30,"carbs":50,"fat":20})
    assert r.status_code==200; assert r.json()["calories"]==500
