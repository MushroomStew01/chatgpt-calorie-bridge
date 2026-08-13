import os
import secrets
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app import fatsecret

BASE_DIR = Path(__file__).resolve().parent

RAW_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./calories.db")
if RAW_DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = RAW_DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif RAW_DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = RAW_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = RAW_DATABASE_URL

APP_API_KEY = os.getenv("APP_API_KEY", "change-me")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Toronto")
LOCAL_TZ = ZoneInfo(APP_TIMEZONE)
DAILY_CALORIE_GOAL = float(os.getenv("DAILY_CALORIE_GOAL", "2000"))
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "andy")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

FATSECRET_CONSUMER_KEY = os.getenv("FATSECRET_CONSUMER_KEY", "")
FATSECRET_CONSUMER_SECRET = os.getenv("FATSECRET_CONSUMER_SECRET", "")
FATSECRET_ACCESS_TOKEN = os.getenv("FATSECRET_ACCESS_TOKEN", "")
FATSECRET_ACCESS_TOKEN_SECRET = os.getenv("FATSECRET_ACCESS_TOKEN_SECRET", "")
FATSECRET_MATCH_MIN_SCORE = float(os.getenv("FATSECRET_MATCH_MIN_SCORE", "0.32"))
FATSECRET_MAX_SEARCH_RESULTS = max(
    1, min(20, int(os.getenv("FATSECRET_MAX_SEARCH_RESULTS", "12")))
)
FATSECRET_DETAIL_CANDIDATES = max(
    1, min(10, int(os.getenv("FATSECRET_DETAIL_CANDIDATES", "6")))
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    eaten_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
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


class FatSecretConnection(Base):
    __tablename__ = "fatsecret_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    request_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    request_token_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    access_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    access_token_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


Base.metadata.create_all(bind=engine)

app = FastAPI(title="ChatGPT Calorie Bridge", version="1.5.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)
dashboard_security = HTTPBasic(auto_error=False)


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
    fatsecret_search_query: Optional[str] = Field(default=None, max_length=200)
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
    if not secrets.compare_digest(x_api_key, APP_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def require_dashboard_access(
    credentials: Optional[HTTPBasicCredentials] = Depends(dashboard_security),
) -> str:
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD is not configured")

    username_ok = bool(credentials) and secrets.compare_digest(
        credentials.username, DASHBOARD_USERNAME
    )
    password_ok = bool(credentials) and secrets.compare_digest(
        credentials.password, DASHBOARD_PASSWORD
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Dashboard authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def fatsecret_keys_configured() -> bool:
    return bool(FATSECRET_CONSUMER_KEY and FATSECRET_CONSUMER_SECRET)


def get_fatsecret_connection(
    db: Session, create: bool = False
) -> Optional[FatSecretConnection]:
    connection = db.get(FatSecretConnection, 1)
    if connection is None and create:
        connection = FatSecretConnection(id=1)
        db.add(connection)
        db.commit()
        db.refresh(connection)
    return connection


def fatsecret_access_credentials(db: Session) -> Optional[tuple[str, str]]:
    connection = get_fatsecret_connection(db)
    if connection and connection.access_token and connection.access_token_secret:
        return connection.access_token, connection.access_token_secret
    if FATSECRET_ACCESS_TOKEN and FATSECRET_ACCESS_TOKEN_SECRET:
        return FATSECRET_ACCESS_TOKEN, FATSECRET_ACCESS_TOKEN_SECRET
    return None


def fatsecret_connected(db: Session) -> bool:
    return fatsecret_keys_configured() and fatsecret_access_credentials(db) is not None


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(LOCAL_TZ)


def local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def days_since_epoch(dt: datetime) -> int:
    local_day = to_local(dt).date()
    return (local_day - date(1970, 1, 1)).days


def public_base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def append_meal_note(meal: Meal, message: str) -> None:
    message = (message or "").strip(" |")
    if not message:
        return
    meal.notes = (f"{meal.notes} | {message}" if meal.notes else message)[:500]


def auto_sync_meal_to_fatsecret(
    db: Session,
    meal: Meal,
    search_query: Optional[str],
) -> Optional[str]:
    credentials = fatsecret_access_credentials(db)
    if not credentials:
        return None

    match = fatsecret.find_best_food_match(
        consumer_key=FATSECRET_CONSUMER_KEY,
        consumer_secret=FATSECRET_CONSUMER_SECRET,
        query=(search_query or meal.name),
        calories=meal.calories,
        protein=meal.protein,
        carbs=meal.carbs,
        fat=meal.fat,
        max_search_results=FATSECRET_MAX_SEARCH_RESULTS,
        max_detail_candidates=FATSECRET_DETAIL_CANDIDATES,
    )
    if match is None:
        append_meal_note(meal, "FatSecret: no matching food found")
        return None
    if match.score < FATSECRET_MATCH_MIN_SCORE:
        append_meal_note(
            meal,
            f"FatSecret: match confidence too low ({match.score:.2f})",
        )
        return None

    access_token, access_token_secret = credentials
    entry_id = fatsecret.create_diary_entry(
        consumer_key=FATSECRET_CONSUMER_KEY,
        consumer_secret=FATSECRET_CONSUMER_SECRET,
        access_token=access_token,
        access_token_secret=access_token_secret,
        food_id=match.food_id,
        serving_id=match.serving_id,
        number_of_units=match.number_of_units,
        food_entry_name=meal.name,
        meal=meal.meal_type,
        date_int=days_since_epoch(meal.eaten_at),
    )

    meal.fatsecret_food_id = match.food_id
    meal.fatsecret_serving_id = match.serving_id
    meal.fatsecret_entry_id = entry_id
    if entry_id:
        append_meal_note(
            meal,
            (
                f"FatSecret synced: {match.display_name}; "
                f"{match.number_of_units:g} × {match.serving_description}; "
                f"match {match.score:.2f}"
            ),
        )
    else:
        append_meal_note(meal, "FatSecret: diary API returned no entry id")
    return entry_id


def sync_explicit_fatsecret_entry(
    db: Session,
    meal: Meal,
    number_of_units: float,
) -> Optional[str]:
    credentials = fatsecret_access_credentials(db)
    if not credentials or not meal.fatsecret_food_id or not meal.fatsecret_serving_id:
        return None
    access_token, access_token_secret = credentials
    entry_id = fatsecret.create_diary_entry(
        consumer_key=FATSECRET_CONSUMER_KEY,
        consumer_secret=FATSECRET_CONSUMER_SECRET,
        access_token=access_token,
        access_token_secret=access_token_secret,
        food_id=meal.fatsecret_food_id,
        serving_id=meal.fatsecret_serving_id,
        number_of_units=number_of_units,
        food_entry_name=meal.name,
        meal=meal.meal_type,
        date_int=days_since_epoch(meal.eaten_at),
    )
    meal.fatsecret_entry_id = entry_id
    if entry_id:
        append_meal_note(meal, "FatSecret synced using supplied food/serving")
    return entry_id


def meal_query_for_day(day: date):
    start, end = day_bounds(day)
    return select(Meal).where(Meal.eaten_at >= start, Meal.eaten_at < end)


def action_schema(base_url: str) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Calorie Bridge",
            "version": "1.5.0",
            "description": (
                "Log estimated meals, automatically match them to FatSecret when "
                "connected, and retrieve daily calorie totals."
            ),
        },
        "servers": [{"url": base_url.rstrip("/")}],
        "paths": {
            "/api/meals": {
                "post": {
                    "operationId": "logMeal",
                    "summary": (
                        "Log a meal with calories/macros and auto-sync the best "
                        "FatSecret food and serving when connected"
                    ),
                    "security": [{"ApiKeyAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name", "calories"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "calories": {"type": "number"},
                                        "protein": {"type": "number", "default": 0},
                                        "carbs": {"type": "number", "default": 0},
                                        "fat": {"type": "number", "default": 0},
                                        "fiber": {"type": "number", "default": 0},
                                        "sugar": {"type": "number", "default": 0},
                                        "meal_type": {
                                            "type": "string",
                                            "enum": [
                                                "breakfast",
                                                "lunch",
                                                "dinner",
                                                "other",
                                            ],
                                            "default": "other",
                                        },
                                        "notes": {"type": "string", "default": ""},
                                        "eaten_at": {
                                            "type": "string",
                                            "format": "date-time",
                                        },
                                        "fatsecret_search_query": {
                                            "type": "string",
                                            "description": (
                                                "Optional concise food name to improve "
                                                "FatSecret matching; omit to use name."
                                            ),
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Meal logged"}},
                },
                "get": {
                    "operationId": "getMeals",
                    "summary": "Get logged meals",
                    "security": [{"ApiKeyAuth": []}],
                    "parameters": [
                        {
                            "in": "query",
                            "name": "day",
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "responses": {"200": {"description": "Meals"}},
                },
            },
            "/api/summary": {
                "get": {
                    "operationId": "getDailySummary",
                    "summary": "Get daily calorie and macro totals",
                    "security": [{"ApiKeyAuth": []}],
                    "parameters": [
                        {
                            "in": "query",
                            "name": "day",
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "responses": {"200": {"description": "Daily summary"}},
                }
            },
        },
        "components": {
            "schemas": {},
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                }
            },
        },
    }


@app.get("/health")
def health(db: Session = Depends(db_session)):
    return {
        "status": "ok",
        "fatsecret_keys_configured": fatsecret_keys_configured(),
        "fatsecret_connected": fatsecret_connected(db),
        "fatsecret_oauth_signer": "manual-rfc3986-hmac-sha1",
        "fatsecret_auto_match": True,
        "fatsecret_match_min_score": FATSECRET_MATCH_MIN_SCORE,
    }


@app.get("/action-openapi.json", include_in_schema=False)
def action_openapi(request: Request):
    return JSONResponse(action_schema(str(request.base_url)))


@app.get("/fatsecret/connect", include_in_schema=False)
def connect_fatsecret(
    request: Request,
    db: Session = Depends(db_session),
    _: str = Depends(require_dashboard_access),
):
    if not fatsecret_keys_configured():
        raise HTTPException(
            status_code=503,
            detail="Set FATSECRET_CONSUMER_KEY and FATSECRET_CONSUMER_SECRET first.",
        )

    callback_url = f"{public_base_url(request)}/fatsecret/callback"
    try:
        token_pair = fatsecret.request_token(
            FATSECRET_CONSUMER_KEY,
            FATSECRET_CONSUMER_SECRET,
            callback_url,
        )
    except fatsecret.FatSecretError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    connection = get_fatsecret_connection(db, create=True)
    connection.request_token = token_pair.token
    connection.request_token_secret = token_pair.secret
    connection.updated_at = datetime.now(timezone.utc)
    db.commit()

    return RedirectResponse(
        fatsecret.authorization_url(token_pair.token),
        status_code=302,
    )


@app.get("/fatsecret/callback", include_in_schema=False)
def fatsecret_callback(
    oauth_token: Optional[str] = None,
    oauth_verifier: Optional[str] = None,
    db: Session = Depends(db_session),
):
    if not oauth_token or not oauth_verifier:
        return RedirectResponse("/?fatsecret=denied", status_code=302)

    connection = get_fatsecret_connection(db)
    if (
        connection is None
        or not connection.request_token
        or not connection.request_token_secret
        or not secrets.compare_digest(oauth_token, connection.request_token)
    ):
        raise HTTPException(
            status_code=400,
            detail="FatSecret OAuth state is invalid or expired.",
        )

    try:
        token_pair = fatsecret.exchange_access_token(
            FATSECRET_CONSUMER_KEY,
            FATSECRET_CONSUMER_SECRET,
            connection.request_token,
            connection.request_token_secret,
            oauth_verifier,
        )
    except fatsecret.FatSecretError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    connection.access_token = token_pair.token
    connection.access_token_secret = token_pair.secret
    connection.request_token = None
    connection.request_token_secret = None
    connection.updated_at = datetime.now(timezone.utc)
    db.commit()

    return RedirectResponse("/?fatsecret=connected", status_code=302)


@app.get("/fatsecret/disconnect", include_in_schema=False)
def disconnect_fatsecret(
    db: Session = Depends(db_session),
    _: str = Depends(require_dashboard_access),
):
    connection = get_fatsecret_connection(db)
    if connection:
        connection.request_token = None
        connection.request_token_secret = None
        connection.access_token = None
        connection.access_token_secret = None
        connection.updated_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/?fatsecret=disconnected", status_code=302)


@app.post(
    "/api/meals",
    response_model=MealOut,
    dependencies=[Depends(require_api_key)],
)
def create_meal(payload: MealCreate, db: Session = Depends(db_session)):
    eaten_at = ensure_utc(payload.eaten_at or datetime.now(timezone.utc))
    meal = Meal(
        name=payload.name,
        calories=payload.calories,
        protein=payload.protein,
        carbs=payload.carbs,
        fat=payload.fat,
        fiber=payload.fiber,
        sugar=payload.sugar,
        meal_type=payload.meal_type,
        notes=payload.notes,
        eaten_at=eaten_at,
        fatsecret_food_id=payload.fatsecret_food_id,
        fatsecret_serving_id=payload.fatsecret_serving_id,
    )
    db.add(meal)
    db.commit()
    db.refresh(meal)

    if fatsecret_connected(db):
        try:
            if payload.fatsecret_food_id and payload.fatsecret_serving_id:
                sync_explicit_fatsecret_entry(
                    db,
                    meal,
                    payload.fatsecret_number_of_units,
                )
            else:
                auto_sync_meal_to_fatsecret(
                    db,
                    meal,
                    payload.fatsecret_search_query,
                )
        except fatsecret.FatSecretError as exc:
            append_meal_note(meal, f"FatSecret sync failed: {exc}")
        except Exception as exc:
            append_meal_note(
                meal,
                f"FatSecret sync failed: {type(exc).__name__}",
            )
        db.commit()
        db.refresh(meal)

    return meal


@app.get(
    "/api/meals",
    response_model=list[MealOut],
    dependencies=[Depends(require_api_key)],
)
def list_meals(
    day: Optional[date] = Query(default=None),
    db: Session = Depends(db_session),
):
    stmt = select(Meal).order_by(Meal.eaten_at.desc())
    if day:
        start, end = day_bounds(day)
        stmt = stmt.where(Meal.eaten_at >= start, Meal.eaten_at < end)
    return list(db.scalars(stmt).all())


@app.get("/api/summary", dependencies=[Depends(require_api_key)])
def summary(
    day: Optional[date] = Query(default=None),
    db: Session = Depends(db_session),
):
    selected = day or local_today()
    start, end = day_bounds(selected)
    stmt = select(
        func.coalesce(func.sum(Meal.calories), 0),
        func.coalesce(func.sum(Meal.protein), 0),
        func.coalesce(func.sum(Meal.carbs), 0),
        func.coalesce(func.sum(Meal.fat), 0),
        func.count(Meal.id),
    ).where(Meal.eaten_at >= start, Meal.eaten_at < end)

    calories, protein, carbs, fat, count = db.execute(stmt).one()
    calories = round(float(calories), 1)
    return {
        "date": selected.isoformat(),
        "timezone": APP_TIMEZONE,
        "meal_count": count,
        "calories": calories,
        "calorie_goal": DAILY_CALORIE_GOAL,
        "calories_remaining": round(DAILY_CALORIE_GOAL - calories, 1),
        "protein": round(float(protein), 1),
        "carbs": round(float(carbs), 1),
        "fat": round(float(fat), 1),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    day: Optional[date] = None,
    db: Session = Depends(db_session),
    _: str = Depends(require_dashboard_access),
):
    selected = day or local_today()
    meals = list(
        db.scalars(
            meal_query_for_day(selected).order_by(Meal.eaten_at.desc())
        ).all()
    )
    calories = round(sum(m.calories for m in meals), 1)
    totals = {
        "calories": calories,
        "goal": DAILY_CALORIE_GOAL,
        "remaining": round(DAILY_CALORIE_GOAL - calories, 1),
        "protein": round(sum(m.protein for m in meals), 1),
        "carbs": round(sum(m.carbs for m in meals), 1),
        "fat": round(sum(m.fat for m in meals), 1),
    }
    return templates.get_template("dashboard.html").render(
        request=request,
        selected=selected,
        meals=meals,
        totals=totals,
        fatsecret_keys_configured=fatsecret_keys_configured(),
        fatsecret_connected=fatsecret_connected(db),
        local_time=to_local,
        timezone_name=APP_TIMEZONE,
    )
