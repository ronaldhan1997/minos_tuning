# Dashboard API

All times are UTC ISO-8601 with an offset. Scores use JSON `null` when missing. A numeric `0` is a real zero. Mock results keep `score_source=mock` and `official_score=null`.

Admin calls accept either:

- `Authorization: Bearer $COORDINATOR_ADMIN_TOKEN` (CLI, scripts, tests). No CSRF header.
- Cookie `minos_session` from `POST /api/v1/auth/login`. Unsafe methods also send `X-CSRF-Token` equal to the login/session `csrf` value.

Workers keep their own bearer tokens and do not use these browser routes.

## Auth

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/auth/login` | `{username, password}`. 401 on a mismatch, 429 after repeated failures, 503 `auth_not_configured` when `DASHBOARD_ADMIN_PASSWORD_HASH` is unset. |
| POST | `/api/v1/auth/logout` | Clears the cookies. |
| GET | `/api/v1/auth/session` | `{authenticated, username, csrf, role}`. |

## Reads

| Method | Path |
| --- | --- |
| GET | `/api/v1/system/status` |
| GET | `/api/v1/overview` |
| GET | `/api/v1/workers?limit&cursor&q` |
| GET | `/api/v1/workers/{worker_id}` |
| GET | `/api/v1/jobs?limit&cursor&experiment_id&worker_id&dataset_id&state&trial_id&q` |
| GET | `/api/v1/jobs/{job_id}` |
| GET | `/api/v1/jobs/{job_id}/logs?tail=` (1–8000 characters) |
| GET | `/api/v1/datasets/registered` |
| GET | `/api/v1/experiments?limit&cursor&q&status` | Paginated object when any of those parameters is set. Otherwise the historical array. |
| GET | `/api/v1/experiments/{key}` | Existing experiment view. `key` is the id or the unique name. |
| GET | `/api/v1/experiments/{key}/trials` | Existing trial rows, including per-dataset jobs. |
| GET | `/api/v1/experiments/{key}/metrics?include_importance=` | Best-so-far in completion order, held-out means, candidates, optional Optuna importance. |
| GET | `/api/v1/experiments/{key}/events` | Audit rows for that experiment id. |
| GET | `/api/v1/audit?limit&cursor` | |
| GET | `/api/v1/search-space` | The on-disk `config/search_space.yaml`. |

List objects are `{items, next_cursor}`. `next_cursor` is null on the last page.

Worker `presence` is one of `online-idle`, `online-busy`, `paused`, `draining`, `incompatible`, `offline`, `unknown`. A heartbeat older than `heartbeat_stale_seconds` (45) is `offline` even if scheduling is paused. `scheduling_mode` is still returned separately.

## Commands

| Method | Path | Effect |
| --- | --- | --- |
| POST | `/api/v1/experiments` | Existing create. Optional `start: false` stores `DRAFT` and does not call Optuna. Default `start: true`. |
| POST | `/api/v1/experiments/{key}/start` | `DRAFT` → `RUNNING`. 409 otherwise. |
| POST | `/api/v1/experiments/{key}/pause` | `RUNNING` or `VALIDATING` → `PAUSED`. Already paused returns the current row. |
| POST | `/api/v1/experiments/{key}/resume` | `PAUSED` → the status stored at pause time. |
| POST | `/api/v1/experiments/{key}/stop` | Stops new work. Leased jobs finish. Unleased jobs become `CANCELLED` / `stopped` and are not numeric scores. With held-out datasets the study ends `STOPPED` and export is refused. Without held-out datasets the existing training selection runs. |
| POST | `/api/v1/experiments/{key}/cancel` | Existing destructive cancel. The dashboard does not call it. |
| POST | `/api/v1/workers/{id}/enable` `/pause` `/drain` | Scheduler assignment only. |
| PATCH | `/api/v1/workers/{id}/policy` | `{max_concurrent_jobs}` at or below the worker's registered parallelism and CPU count, or `{reset: true}`. |
| POST | `/api/v1/jobs/{id}/retry` | Only `FAILED` or `TIMED_OUT`, trial not yet told, attempts remaining, error class not in the non-retryable set, experiment `RUNNING`, `VALIDATING`, or `PAUSED`. |
| POST | `/api/v1/experiments/{key}/export` | Stores `gatk.conf` and provenance after the existing export check. |
| GET | `/api/v1/exports/{id}/download?part=conf\|provenance` | Attachment download. No filesystem path is accepted. |
| POST | `/api/v1/datasets/import` | Existing manifest import. |
| POST | `/api/v1/datasets/import-practice` | `{sample_path, scoring_version}`. `sample_path` must resolve under `MINOS_DATASET_ROOTS` or `<repo>/datasets`. |

Repeat a command with the same `Idempotency-Key` header to get the current resource without applying it twice. Pause of an already paused study is also a no-op success.

Errors stay `{error, message}` with 409 for illegal transitions, 401 for auth, 403 for CSRF.
