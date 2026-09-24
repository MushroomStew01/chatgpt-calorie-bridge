"""Durable single-meal outbox. Ambiguous diary writes are reconciled, never replayed."""
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import select, update, or_

from app import fatsecret

ACTIVE = ("pending", "retrying", "verifying", "writing")


class NotConnected(fatsecret.FatSecretError):
    pass


def now():
    return datetime.now(timezone.utc)


def enqueue(db, meal, legacy=False):
    from app.main import SyncJob
    job = db.get(SyncJob, meal.id)
    if job is None:
        job = SyncJob(meal_id=meal.id, marker=secrets.token_hex(12),
                      state="needs_review" if legacy else "pending",
                      error="Older meal: inspect the FatSecret diary before retrying." if legacy else "",
                      next_attempt=now())
        db.add(job)
    return job


def status(db, meal):
    from app.main import SyncJob
    job = db.get(SyncJob, meal.id)
    return {"local_saved": True, "meal_id": meal.id, "fatsecret": {
        "status": job.state if job else "unverified",
        "entry_id": meal.fatsecret_entry_id,
        "verified_at": job.verified_at.isoformat() if job and job.verified_at else None,
        "error": job.error if job else "Legacy entry has not been verified by the new sync worker.",
        "attempts": job.attempts if job else 0,
    }}


def auth(db):
    from app import main
    credentials = main.fatsecret_access_credentials(db)
    if not main.fatsecret_keys_configured() or not credentials:
        raise NotConnected("FatSecret is not connected. Reconnect from the dashboard.")
    return dict(consumer_key=main.FATSECRET_CONSUMER_KEY,
                consumer_secret=main.FATSECRET_CONSUMER_SECRET,
                access_token=credentials[0], access_token_secret=credentials[1])


def reconcile(db, meal, job, credentials):
    from app.main import days_since_epoch
    entries = fatsecret.read_diary_entries(**credentials, date_int=days_since_epoch(meal.eaten_at))
    marker = f"[CB:{job.marker}]"
    matches = [e for e in entries if (
        (meal.fatsecret_entry_id and str(e.get("food_entry_id")) == meal.fatsecret_entry_id)
        or marker in str(e.get("food_entry_name", "")))]
    if not matches:
        job.state = "verifying"
        job.error = "Diary entry not yet found. Read-back will retry; no duplicate write will be sent."
        job.verified_at = None
        return
    if len(matches) != 1:
        job.state = "needs_review"
        job.error = "Multiple diary entries match this meal; review required."
        job.verified_at = None
        return
    entry = matches[0]
    entry_id = str(entry.get("food_entry_id") or "")
    if not entry_id.isdigit() or int(entry_id) <= 0:
        raise fatsecret.FatSecretError("Diary read-back returned an invalid entry id")
    meal.fatsecret_entry_id = entry_id
    valid = (
        fatsecret._decimal(entry.get("calories")) == fatsecret._decimal(meal.calories)
        and str(entry.get("date_int")) == str(days_since_epoch(meal.eaten_at))
        and str(entry.get("meal", "")).lower() == meal.meal_type
        and (not meal.fatsecret_food_id or str(entry.get("food_id")) == meal.fatsecret_food_id)
        and (not meal.fatsecret_serving_id or str(entry.get("serving_id")) == meal.fatsecret_serving_id)
    )
    job.state = "verified" if valid else "needs_review"
    job.error = "" if valid else "FatSecret diary calories, date, meal or food differ from the saved meal."
    job.verified_at = now() if valid else None


def process(meal_id, force=False):
    from app.main import SessionLocal, Meal, SyncJob, days_since_epoch
    # Claim before any network call. An expired lease recovers crashed workers;
    # write_started remains durable across crashes and forces read-only recovery.
    lease = secrets.token_hex(16)
    with SessionLocal() as db:
        filters = [SyncJob.meal_id == meal_id,
                   or_(SyncJob.lease_until.is_(None), SyncJob.lease_until < now())]
        if not force:
            filters += [SyncJob.state.in_(ACTIVE), SyncJob.next_attempt <= now()]
        claimed = db.execute(update(SyncJob).where(*filters).values(
            lease=lease, lease_until=now() + timedelta(minutes=5)))
        db.commit()
        if claimed.rowcount != 1:
            return
        job, meal = db.get(SyncJob, meal_id), db.get(Meal, meal_id)
        if meal is None:
            job.state, job.error = "needs_review", "Local meal no longer exists."
            job.lease, job.lease_until = None, None
            db.commit()
            return
        job.attempts += 1
        if job.write_started or meal.fatsecret_entry_id:
            job.state, job.verified_at = "verifying", None
        db.commit()
        try:
            credentials = auth(db)
            if job.write_started or meal.fatsecret_entry_id:
                reconcile(db, meal, job, credentials)
            elif job.state == "needs_review":
                return
            else:
                if not job.food_ready:
                    food_id, serving_id = fatsecret.create_exact_food(
                        **credentials, name=meal.name, calories=meal.calories,
                        protein=meal.protein, carbs=meal.carbs, fat=meal.fat,
                        fiber=meal.fiber, sugar=meal.sugar)
                    meal.fatsecret_food_id, meal.fatsecret_serving_id = food_id, serving_id
                    job.food_ready = True
                    db.commit()
                # Commit this BEFORE posting. Even a process crash cannot cause
                # an automatic duplicate diary write after restart.
                job.write_started, job.state = True, "writing"
                db.commit()
                try:
                    meal.fatsecret_entry_id = fatsecret.post_diary_entry(
                        **credentials, food_id=meal.fatsecret_food_id,
                        serving_id=meal.fatsecret_serving_id,
                        food_entry_name=f"{meal.name[:160]} [CB:{job.marker}]",
                        meal=meal.meal_type, date_int=days_since_epoch(meal.eaten_at))
                except fatsecret.FatSecretAPIError:
                    # An explicit API rejection is a known failed write.
                    job.write_started = False
                    raise
                except (fatsecret.FatSecretError, requests.RequestException):
                    # Missing ID, malformed response or timeout: look for marker.
                    pass
                job.state = "verifying"
                db.commit()
                reconcile(db, meal, job, credentials)
        except NotConnected:
            job.state, job.verified_at = "retrying", None
            job.error = "FatSecret is not connected. Reconnect from the dashboard; sync will resume."
        except fatsecret.FatSecretAPIError as exc:
            job.state = "retrying" if exc.code in {"6", "7", "12", "HTTP429"} else "blocked"
            job.error = str(exc) + ("; reconnect/check API permissions." if job.state == "blocked" else "; will retry.")
            job.verified_at = None
        except (fatsecret.FatSecretError, requests.RequestException) as exc:
            job.state = "verifying" if job.write_started else "retrying"
            # Never copy response bodies, signed URLs or credentials into status.
            job.error = "FatSecret check failed (" + type(exc).__name__ + "); will retry safely."
            job.verified_at = None
        except Exception:
            job.state = "verifying" if job.write_started else "retrying"
            job.error = "Sync worker failed; queued for safe recovery."
            job.verified_at = None
            logging.getLogger(__name__).error("Sync worker failed for meal %s", meal_id)
        finally:
            job.next_attempt = now() + timedelta(seconds=min(3600, 15 * 2 ** min(job.attempts, 8)))
            job.lease = None
            job.lease_until = None
            db.commit()


def start_worker():
    from app.main import SessionLocal, SyncJob
    stop = threading.Event()
    def run():
        while not stop.is_set():
            try:
                with SessionLocal() as db:
                    ids = list(db.scalars(select(SyncJob.meal_id).where(
                        SyncJob.state.in_(ACTIVE), SyncJob.next_attempt <= now()).limit(20)))
                for meal_id in ids:
                    if stop.is_set():
                        break
                    process(meal_id)
            except Exception:
                logging.getLogger(__name__).error("Sync queue unavailable; retrying shortly")
            stop.wait(5)
    thread = threading.Thread(target=run, name="fatsecret-outbox", daemon=True)
    thread.start()
    return stop, thread
