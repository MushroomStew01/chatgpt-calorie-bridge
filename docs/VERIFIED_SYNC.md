# Verified FatSecret syncing

This source provides tracker 1.7.2, MCP adapter 1.1.0 and plugin instructions 0.7.0.
Upgrade the tracker and MCP adapter together using the procedure below. These
source versions do not establish what is currently deployed. Earlier deployed
branches used catalog search and a one-shot background task. Merging a fix in
GitHub does not change a running Docker container.

## What is confirmed

The meal and a sync job commit in the same database transaction. A worker scans
the durable queue every five seconds and resumes after a restart. It creates a
custom food containing the supplied calories and macros. If custom creation is
unavailable (method/scope/access rejection), it selects a matching generic catalog
food and scales the serving to the logged calories. Food and serving IDs, units,
and preparation mode are persisted before the diary POST. Catalog macros may
differ; the status response exposes this distinction.

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

## Catalog calorie precision policy

Catalog portions are rounded to four decimal places before nutrition is predicted.
A candidate whose resulting calories differ by more than **0.5 kcal absolute**
is rejected before the diary POST. Read-back uses the same maximum absolute
difference for catalog entries only, accommodating whole-kcal display rounding.
This is an application acceptance policy, not a guarantee about FatSecret's
serialization. It does not grow with meal size. Values outside the bound still
require review; custom-food calories still require exact equality.

Zero-calorie meals require an exact custom food. If custom creation is unavailable,
they remain retrying locally without a catalog diary POST. A previously prepared
zero-calorie catalog job is also prevented from posting. Existing attempted writes
remain read-only reconciliation jobs.

Date, meal, food ID and serving ID checks remain strict (meal case is normalized;
no undocumented aliases are accepted). Status includes `calorie_tolerance_kcal`
and `mismatch_fields` with fixed names such as `calories` or `serving_id`.
No provider response bodies, signed URLs or tokens appear in those diagnostics.
A mismatch is not evidence of its cause until the failed field is known.

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

For an existing installation with both `calorie-bridge-app` and
`calorie-work-mcp`, use a fresh checkout of the reviewed commit on the Pi.
The script requires the existing MCP container and its persistent config/data.
For a fresh installation, first follow [Raspberry Pi setup](RASPBERRY_PI.md),
then [MCP installation](CHATGPT_WORK.md). Existing MCP state must not be deleted.

From the checkout:

```bash
bash scripts/pi-deploy-verified-sync.sh
```

The script backs up the database, environment and running source, builds both
images, preserves the existing Compose project/database volume and actual MCP
config/data mounts, and checks both service versions. The tracker health check uses its existing
Docker-published host port, including nondefault configurations. It leaves Funnel routing
alone. Health checks prove the new code is running, **not** that FatSecret accepted
a meal. Check an authorized meal through `getMealSyncStatus` afterward. No test
meal is injected by deployment.

The `fatsecret_sync_jobs` and `fatsecret_sync_preparations` tables are additive;
existing meal columns are unchanged.
Use one Uvicorn worker on the Pi (the supplied Dockerfiles do). Atomic expiring
leases also prevent two outbox workers from posting the same job concurrently.

## Provider limitations

FatSecret must be connected. Custom creation is preferred but is not required
when a suitable catalog food is available. A missing suitable match remains queued.
An outage, diary access denial or provider calorie rounding cannot be solved by
claiming success. These remain visible pending/blocked/review states.

Unknown-method error 10 retries the documented RPC format once; it is not labeled
as a login error. Other errors and uncertain writes do not trigger a second POST.

Provider contracts checked:
- https://platform.fatsecret.com/docs/v2/food.create
- https://platform.fatsecret.com/docs/v1/food_entry.create
- https://platform.fatsecret.com/docs/v2/food_entries.get

Tests cover accepted-but-timed-out writes, response formats, delayed visibility,
crash recovery, active leases, explicit rejections, calorie/date/identity mismatch,
auth protection, transactional persistence, MCP idempotency, stale verification,
method-format fallback, catalog units persistence, rate limits and provider timeouts.
