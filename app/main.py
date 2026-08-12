import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from requests_oauthlib import OAuth1
from sqlalchemy import DateTime, Float, Integer, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./calories.db")
APP_API_KEY = os.getenv("APP_API_KEY", "change-me")
FATSECRET_CONSUMER_KEY = os.getenv("FATSECRET_CONSUMER_KEY", "")
FATSECRET_CONSUMER_SECRET = os.getenv("FATSECRET_CONSUMER_SECRET", "")
FATSECRET_ACCESS_TOKEN = os.getenv("FATSECRET_ACCESS_TOKEN", "")
FATSECRET_ACCESS_TOKEN_SECRET = os.getenv("FATSECRET_ACCESS_TOKEN_SECRET", "")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Meal(Base):
    __tablename__ = "meals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    eaten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    name: Mapped[str] = mapped_column(String(200))
    calories: Mapped[float] = mapped_column(Float)
    protein: Mapped[float] = mapped_column(Float, default=0)
    carbs: Mapped[float] = mapped_column(Float, default=0)
    fat: Mapped[float] = mapped_column(Float, default=0)
    fiber: Mapped[float] = mapped_column(Float, default=0)
    sugar: Mapped[float] = mapped_column(Float, default=0)
    meal_type: Mapped[str] = mapped_column(String(20), default="other")
    notes: Mapped[str] = mapped_column(String(500), default="")
    fatsecret_food_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    fatsecret_serving_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    fatsecret_entry_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

Base.metadata.create_all(bind=engine)
app = FastAPI(title="ChatGPT Calorie Bridge", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Environment(loader=FileSystemLoader(BASE_DIR / "templates"), autoescape=select_autoescape(["html", "xml"]))

class MealCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    calories: float = Field(ge=0)
    protein: float = Field(default=0, ge=0)
    carbs: float = Field(default=0, ge=0)
    fat: float = Field(default=0, ge=0)
    fiber: float = Field(default=0, ge=0)
    sugar: float = Field(default=0, ge=0)
    meal_type: str = Field(default="other", pattern="^(breakfast|lunch|dinner|other)$")
    notes: str = Field(default="", max_length=500)
    eaten_at: Optional[datetime] = None
    fatsecret_food_id: Optional[str] = None
    fatsecret_serving_id: Optional[str] = None
    fatsecret_number_of_units: float = Field(default=1.0, gt=0)

class MealOut(BaseModel):
    id: int
    created_at: datetime
    eaten_at: datetime
    name: str
    calories: float
    protein: float
    carbs: float
    fat: float
    fiber: float
    sugar: float
    meal_type: str
    notes: str
    fatsecret_entry_id: Optional[str] = None
    model_config = {"from_attributes": True}

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_api_key(x_api_key: str = Header(default="")):
    if not APP_API_KEY or APP_API_KEY == "change-me":
        raise HTTPException(status_code=503, detail="APP_API_KEY is not configured")
    if x_api_key != APP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

def fatsecret_ready() -> bool:
    return all([FATSECRET_CONSUMER_KEY, FATSECRET_CONSUMER_SECRET, FATSECRET_ACCESS_TOKEN, FATSECRET_ACCESS_TOKEN_SECRET])

def days_since_epoch(dt: datetime) -> int:
    return (dt.date() - date(1970, 1, 1)).days

def sync_to_fatsecret(meal: Meal, number_of_units: float = 1.0) -> Optional[str]:
    if not fatsecret_ready() or not meal.fatsecret_food_id or not meal.fatsecret_serving_id:
        return None
    auth = OAuth1(
        FATSECRET_CONSUMER_KEY,
        client_secret=FATSECRET_CONSUMER_SECRET,
        resource_owner_key=FATSECRET_ACCESS_TOKEN,
        resource_owner_secret=FATSECRET_ACCESS_TOKEN_SECRET,
        signature_method="HMAC-SHA1",
    )
    payload = {
        "food_id": meal.fatsecret_food_id,
        "food_entry_name": meal.name,
        "serving_id": meal.fatsecret_serving_id,
        "number_of_units": number_of_units,
        "meal": meal.meal_type,
        "date": days_since_epoch(meal.eaten_at),
        "format": "json",
    }
    response = requests.post("https://platform.fatsecret.com/rest/food-entries/v1", data=payload, auth=auth, timeout=20)
    response.raise_for_status()
    body = response.json()
    entry = body.get("food_entries", {}).get("food_entry")
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    return str(entry.get("food_entry_id")) if isinstance(entry, dict) and entry.get("food_entry_id") else None

def day_bounds(day: date):
    return datetime.combine(day, time.min, tzinfo=timezone.utc), datetime.combine(day, time.max, tzinfo=timezone.utc)

@app.get("/health")
def health():
    return {"status": "ok", "fatsecret_connected": fatsecret_ready()}

@app.post("/api/meals", response_model=MealOut, dependencies=[Depends(require_api_key)])
def create_meal(payload: MealCreate, db: Session = Depends(db_session)):
    eaten_at = payload.eaten_at or datetime.now(timezone.utc)
    if eaten_at.tzinfo is None:
        eaten_at = eaten_at.replace(tzinfo=timezone.utc)
    meal = Meal(
        name=payload.name, calories=payload.calories, protein=payload.protein,
        carbs=payload.carbs, fat=payload.fat, fiber=payload.fiber, sugar=payload.sugar,
        meal_type=payload.meal_type, notes=payload.notes, eaten_at=eaten_at,
        fatsecret_food_id=payload.fatsecret_food_id, fatsecret_serving_id=payload.fatsecret_serving_id,
    )
    db.add(meal); db.commit(); db.refresh(meal)
    if payload.fatsecret_food_id and payload.fatsecret_serving_id:
        try:
            meal.fatsecret_entry_id = sync_to_fatsecret(meal, payload.fatsecret_number_of_units)
        except Exception as exc:
            meal.notes = (meal.notes + f" | FatSecret sync failed: {exc}").strip(" |")[:500]
        db.commit(); db.refresh(meal)
    return meal

@app.get("/api/meals", response_model=list[MealOut])
def list_meals(day: Optional[date] = Query(default=None), db: Session = Depends(db_session)):
    stmt = select(Meal).order_by(Meal.eaten_at.desc())
    if day:
        start, end = day_bounds(day)
        stmt = stmt.where(Meal.eaten_at >= start, Meal.eaten_at <= end)
    return list(db.scalars(stmt).all())

@app.get("/api/summary")
def summary(day: Optional[date] = Query(default=None), db: Session = Depends(db_session)):
    selected = day or date.today()
    start, end = day_bounds(selected)
    stmt = select(
        func.coalesce(func.sum(Meal.calories), 0), func.coalesce(func.sum(Meal.protein), 0),
        func.coalesce(func.sum(Meal.carbs), 0), func.coalesce(func.sum(Meal.fat), 0), func.count(Meal.id)
    ).where(Meal.eaten_at >= start, Meal.eaten_at <= end)
    calories, protein, carbs, fat, count = db.execute(stmt).one()
    return {"date": selected.isoformat(), "meal_count": count, "calories": round(float(calories), 1),
            "protein": round(float(protein), 1), "carbs": round(float(carbs), 1), "fat": round(float(fat), 1)}

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, day: Optional[date] = None, db: Session = Depends(db_session)):
    selected = day or date.today()
    start, end = day_bounds(selected)
    meals = list(db.scalars(select(Meal).where(Meal.eaten_at >= start, Meal.eaten_at <= end).order_by(Meal.eaten_at.desc())).all())
    totals = {"calories": round(sum(m.calories for m in meals),1), "protein": round(sum(m.protein for m in meals),1),
              "carbs": round(sum(m.carbs for m in meals),1), "fat": round(sum(m.fat for m in meals),1)}
    return templates.get_template("dashboard.html").render(request=request, selected=selected, meals=meals, totals=totals, fatsecret_connected=fatsecret_ready())
