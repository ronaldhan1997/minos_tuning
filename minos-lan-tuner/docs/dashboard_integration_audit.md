# Dashboard integration audit

Inspected the coordinator in this tree before adding the control plane. Pinned Minos commit remains `4b3c0944b517d09be9b54eb8fa3158ae700b5d5f`. There is no `MINOS_DISTRIBUTED_TUNER_CURSOR_PROMPT.md` in the checkout.

## What already existed

| UI need | Source |
| --- | --- |
| Worker register, heartbeat, claim, complete, fail, release | `POST /api/v1/workers/register`, `/workers/heartbeat`, `/jobs/claim`, `/jobs/{id}/complete`, `/fail`, `/release` |
| Experiment create, list, get, pause, resume, cancel, leaderboard, trials, conf text | `/api/v1/experiments` and the existing subpaths |
| Dataset import and manifest | `POST /api/v1/datasets/import`, `GET /api/v1/datasets/{id}/manifest` |
| Optuna ask/tell once, leases, held-out selection | `apps/coordinator/service.py` |
| Scores | `official_score` or `mock_objective`, never mixed. Failures stay null. |
| Admin auth | Bearer `COORDINATOR_ADMIN_TOKEN`, plus loopback bypass when `MINOS_ALLOW_LOOPBACK_ADMIN` is not `0` |
| Status page | Server-rendered HTML from `apps/coordinator/dashboard.py` |

Experiment states that already existed: `RUNNING` (create starts immediately), `PAUSED`, `VALIDATING`, `COMPLETED`, `FAILED`, `CANCELLED`. Pause already stops new claims and leaves leased jobs alone. Cancel marks active jobs `CANCELLED` and tells Optuna `PRUNED`. The worker heartbeat returns `cancelled_job_ids`, and `apps/worker/runner.py` ignores that field, so a cancel does not stop the GATK container.

Workers had no scheduling mode. Job `log_excerpt` was accepted on fail and then dropped. There was no audit table, no browser session, and no pagination.

## What this change adds

- Browser login: PBKDF2 password hash, HMAC session cookie, CSRF header on cookie writes. Bearer tokens stay valid for `minosctl` and workers and do not send CSRF.
- Read models: `/api/v1/overview`, `/system/status`, `/workers`, `/jobs`, `/datasets/registered`, `/experiments/{id}/metrics`, `/events`, `/audit`.
- Commands: worker `enable`, `pause`, `drain`, `PATCH /policy`; experiment `start` (draft only) and `stop` (graceful). Optional `start: false` on create leaves status `DRAFT`.
- `STOPPING` / `STOPPED`. Pending unleashed jobs are cancelled with `error_class=stopped`. Leased jobs finish. Held-out studies that stop early do not export.
- `job_events`, `audit_events`, `export_records`. Alembic `0002_dashboard` plus a startup column check.
- Compiled React app served from `apps/dashboard/dist`. The old HTML page moved to `/legacy` and requires the same admin auth.

## Deviations from the proposed contract

- `GET /api/v1/experiments` without query parameters is still a JSON array, so the existing CLI keeps working. `limit`, `cursor`, `q`, or `status` switches it to `{items, next_cursor}`.
- There is no `draft → queued` queue. A draft starts straight to `RUNNING` on `POST /start`. Create still defaults to starting immediately.
- Cancel is not offered in the dashboard. The API route remains for the CLI. The worker does not kill Docker on cancel, so the button is disabled and the reason is shown.
- Unvalidated export is not a bypass. Held-out configured but unfinished returns 409 `not_validated`. A completed study with no held-out datasets exports, with `research_only: true`.
- No SSE. The UI polls every 4 seconds and pauses while the tab is hidden.
- No second role. The signed-in user is `admin`.
- Worker runtime text for Windows says Docker Desktop/WSL2 is required. The agent does not report which one is active.
- `GET /health` was not added. `/healthz` and `/readyz` stay as they were.
- Parameter importance is computed only when `include_importance=true`, and only when Optuna can calculate it.

## Metrics and where they come from

| Card or control | Data |
| --- | --- |
| Workers online / total | Heartbeat age ≤ 45s, not `offline` or `unknown` |
| Active / queued / failed jobs | `jobs.state` counts |
| Finished / budget | `trials.told` / `experiments.budget` for the active study |
| Best validated | `selection.held_out_mean` on a `COMPLETED` study, else the label Not validated |
| Best training | Selection or the highest told training objective. Labeled not validated unless a held-out mean exists |
| Worker presence | Heartbeat, `docker_ready`, commit versus coordinator pin, `scheduling_mode`, active leases |
| Pause / drain / enable | `workers.scheduling_mode`. Claim skips `paused` and `draining` |
| Export | `export_conf` after `COMPLETED`, then a stored copy |
