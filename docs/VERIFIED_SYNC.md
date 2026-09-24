# Verified FatSecret syncing

Tracker 1.7.0 and MCP adapter 1.1.0 must be deployed together. Earlier deployed
branches used catalog search and a one-shot background task. Merging a fix in
GitHub does not change a running Docker container.

## What is confirmed

The meal and a sync job commit in the same database transaction. A worker scans
the durable queue every five seconds and resumes after a restart. It creates a
custom food containing the supplied calories and macros; no catalog food match
is required. Food and serving IDs are persisted before the diary POST.

Before that POST, the worker commits `write_started` and embeds a stable random
correlation marker in the diary name. After the POST it independently reads
the diary for the meal's Toronto date, checks the unique entry, and compares
calories, date, meal type, food ID and serving ID. Only this successful read-back
sets `verified`, with a verification timestamp. Macro inputs are preserved in
the custom food; diary verification currently compares calories, not every macro.

The authenticated `GET /api/meals/{id}/verification` reports the Pi save and
FatSecret status separately. Once a write has been attempted, it re-reads the
diary; a failed check clears old verification. It never creates a diary entry.
`POST /api/meals/{id}/retry-sync` requeues the existing job without adding a meal.
The MCP adapter exposes these as `getMealSyncStatus` and `retryMealSync` and checks
verification itself after `logMeal`, including cached idempotent responses.

## States and recovery

- `pending`: durable job is waiting for the worker.
- `retrying`: no uncertain diary write; transient errors retry with exponential
  backoff, capped at one hour. Reconnecting restores work automatically.
- `writing` / `verifying`: a write may exist; the worker only reads back until
  it finds the correlated entry. It never automatically repeats an ambiguous POST.
- `verified`: a unique diary entry was read back with matching calories and identity.
- `blocked`: authorization, API permission or permanent API rejection needs repair;
  call `retryMealSync` after repairing it.
- `needs_review`: duplicate markers, mismatched diary data, or an old untracked
  write needs inspection. The system neither deletes nor blindly reposts entries.
- `unverified`: a legacy record or unavailable verification endpoint.

An empty diary after a timeout does **not** prove the POST failed: visibility can
be delayed. Such jobs remain in `verifying`; this deliberately favors avoiding
duplicate calorie entries over automatic resubmission. Custom-food creation can
leave an unused custom food after a timeout, but cannot double-count diary calories.

Existing meals without jobs are not replayed on upgrade. Their old errors may
mean that a diary entry was created despite a missing response ID. Review the
actual diary before any repair. Existing entry IDs alone are not proof of the new
read-back verification.

## Deployment

From a fresh checkout of the reviewed branch on the Pi:

```bash
bash scripts/pi-deploy-verified-sync.sh
```

The script backs up the database, environment and running source, builds both
images, preserves the existing Compose project/database volume and actual MCP
config/data mounts, and checks both service versions. It leaves Funnel routing
alone. Health checks prove the new code is running, **not** that FatSecret accepted
a meal. Check an authorized meal through `getMealSyncStatus` afterward. No test
meal is injected by deployment.

The new `fatsecret_sync_jobs` table is additive; existing meal columns are unchanged.
Use one Uvicorn worker on the Pi (the supplied Dockerfiles do). Atomic expiring
leases also prevent two outbox workers from posting the same job concurrently.

## Provider limitations

FatSecret must be connected and the account's API app must have `food.create.v2`
permission (documented as Premier Exclusive). An outage, permission denial or
provider calorie rounding cannot be solved by claiming success or substituting
an inaccurate catalog entry. These remain visible pending/blocked/review states.

Provider contracts checked:
- https://platform.fatsecret.com/docs/v2/food.create
- https://platform.fatsecret.com/docs/v1/food_entry.create
- https://platform.fatsecret.com/docs/v2/food_entries.get

Tests cover accepted-but-timed-out writes, response formats, delayed visibility,
crash recovery, active leases, explicit rejections, calorie/date/identity mismatch,
auth protection, transactional persistence, MCP idempotency and stale verification.
