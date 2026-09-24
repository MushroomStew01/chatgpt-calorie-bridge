import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, String, create_engine, func, select
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


class SyncPreparation(Base):
    __tablename__ = "fatsecret_sync_preparations"
    meal_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    units: Mapped[float] = mapped_column(Float, default=1)
    mode: Mapped[str] = mapped_column(String(30), default="custom")
    catalog_name: Mapped[str] = mapped_column(String(200), default="")


class SyncJob(Base):
    __tablename__ = "fatsecret_sync_jobs"
    meal_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marker: Mapped[str] = mapped_column(String(32), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str] = mapped_column(String(500), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    food_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    write_started: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app):
    from app.sync import start_worker
    stop, thread = start_worker()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)

app = FastAPI(title="ChatGPT Calorie Bridge", version="1.7.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)
dashboard_security = HTTPBasic(auto_error=False)


class MealCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    calories: float = Field(ge=0, allow_inf_nan=False)
    protein: float = Field(default=0, ge=0, allow_inf_nan=False)
    carbs: float = Field(default=0, ge=0, allow_inf_nan=False)
    fat: float = Field(default=0, ge=0, allow_inf_nan=False)
    fiber: float = Field(default=0, ge=0, allow_inf_nan=False)
    sugar: float = Field(default=0, ge=0, allow_inf_nan=False)
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
    sync: Optional[dict] = None

    model_config = {"from_attributes": True}


def meal_payload(db, meal):
    from app.sync import status
    return {**MealOut.model_validate(meal).model_dump(), "sync": status(db, meal)}


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
    # Reserve space for the latest result even when the user's notes are full.
    message = message[:497]
    available = 500 - len(message) - 3
    prefix = (meal.notes or "")[:available]
    meal.notes = f"{prefix} | {message}" if prefix else message


def meal_query_for_day(day: date):
    start, end = day_bounds(day)
    return select(Meal).where(Meal.eaten_at >= start, Meal.eaten_at < end)


def action_schema(base_url: str) -> dict:
    schema = {
        "openapi": "3.1.0",
        "info": {
            "title": "Calorie Bridge",
            "version": "1.7.1",
            "description": (
                "Log meals and sync their exact calories to FatSecret when "
                "connected, and retrieve daily calorie totals."
            ),
        },
        "servers": [{"url": base_url.rstrip("/")}],
        "paths": {
            "/api/meals": {
                "post": {
                    "operationId": "logMeal",
                    "x-openai-isConsequential": False,
                    "summary": (
                        "Log a meal immediately and queue FatSecret synchronization "
                        "in the background when connected"
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
                                                "Legacy input; ignored by exact-calorie sync."
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
                    "summary": "Get meals for one day; defaults to today",
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
    schema["paths"]["/api/meals/{meal_id}/verification"] = {"get": {
        "operationId": "getMealSyncStatus",
        "summary": "Mandatory after logging: check Pi and FatSecret separately; only verified confirms sync",
        "security": [{"ApiKeyAuth": []}],
        "parameters": [{"in": "path", "name": "meal_id", "required": True,
                        "schema": {"type": "integer", "minimum": 1}}],
        "responses": {"200": {"description": "Local save and FatSecret read-back status"}},
    }}
    schema["paths"]["/api/meals/{meal_id}/retry-sync"] = {"post": {
        "operationId": "retryMealSync",
        "summary": "Retry sync for an existing meal without logging a duplicate",
        "security": [{"ApiKeyAuth": []}],
        "parameters": [{"in": "path", "name": "meal_id", "required": True,
                        "schema": {"type": "integer", "minimum": 1}}],
        "responses": {"200": {"description": "Sync queued or manual review required"}},
    }}
    return schema


@app.get("/health")
def health(db: Session = Depends(db_session)):
    return {
        "status": "ok",
        "api_version": "1.7.1",
        "fatsecret_keys_configured": fatsecret_keys_configured(),
        "fatsecret_connected": fatsecret_connected(db),
        "fatsecret_oauth_signer": "manual-rfc3986-hmac-sha1",
        "fatsecret_auto_match": False,
        "fatsecret_exact_calories": True,
        "fatsecret_sync_mode": "durable-outbox-readback",
        "fatsecret_match_min_score": FATSECRET_MATCH_MIN_SCORE,
        "get_meals_default_scope": "today",
    }


@app.get("/action-openapi.json", include_in_schema=False)
def action_openapi(request: Request):
    return JSONResponse(action_schema(public_base_url(request)))


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
def create_meal(
    payload: MealCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(db_session),
):
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
    from app.sync import enqueue
    # Meal and job commit together. A restart cannot lose queued sync work.
    db.add(meal)
    db.flush()
    enqueue(db, meal)
    db.commit()
    db.refresh(meal)
    return meal_payload(db, meal)


@app.get(
    "/api/meals",
    response_model=list[MealOut],
    dependencies=[Depends(require_api_key)],
)
def list_meals(
    day: Optional[date] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=50),
    db: Session = Depends(db_session),
):
    # Returning the entire lifetime history eventually exceeded the action
    # response-size limit. Default to today and hard-cap the response.
    selected = day or local_today()
    stmt = meal_query_for_day(selected).order_by(Meal.eaten_at.desc()).limit(limit)
    return [meal_payload(db, meal) for meal in db.scalars(stmt).all()]


@app.get("/api/meals/{meal_id}/verification", dependencies=[Depends(require_api_key)])
def verify_meal(meal_id: int, db: Session = Depends(db_session)):
    from app.sync import process, status
    meal = db.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404, "Meal not found")
    job = db.get(SyncJob, meal_id)
    # Existing writes are checked against the provider. This endpoint never
    # creates a food or diary entry. Pending jobs are handled by the worker.
    if job and (job.write_started or meal.fatsecret_entry_id):
        process(meal_id, force=True)
        db.expire_all()
        meal = db.get(Meal, meal_id)
    return {"meal": meal_payload(db, meal), **status(db, meal)}


@app.post("/api/meals/{meal_id}/retry-sync", dependencies=[Depends(require_api_key)])
def retry_sync(meal_id: int, db: Session = Depends(db_session)):
    from app.sync import enqueue, now, status
    meal = db.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404, "Meal not found")
    job = db.get(SyncJob, meal_id)
    if job is None:
        # Old writes had no correlation marker. Never blindly replay them.
        job = enqueue(db, meal, legacy=True)
    elif job.state not in {"verified", "needs_review"}:
        job.state = "verifying" if job.write_started else "pending"
        job.next_attempt = now()
    db.commit()
    return status(db, meal)


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
    from app.sync import status
    sync_states = {m.id: status(db, m)["fatsecret"] for m in meals}
    return templates.get_template("dashboard.html").render(
        request=request,
        selected=selected,
        meals=meals,
        sync_states=sync_states,
        totals=totals,
        fatsecret_keys_configured=fatsecret_keys_configured(),
        fatsecret_connected=fatsecret_connected(db),
        local_time=to_local,
        timezone_name=APP_TIMEZONE,
    )
