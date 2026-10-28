# Dashboard test report

Command, from `tuning/minos_tuning/minos-lan-tuner`:

```bash
python -m pytest tests -q -m "not real_minos"
```

Result: **29 passed, 1 deselected, 1 warning** in about 20 seconds. The deselected test is `real_minos` (official GATK + hap.py). The warning is Starlette noting that `httpx` with `TestClient` is deprecated.

## What the new tests actually did

`tests/test_dashboard_api.py` uses the in-process coordinator and mock execution:

- Login rejects a bad password, accepts the PBKDF2 password, and requires the CSRF header on a cookie write. Bearer admin still works without CSRF.
- The fourth failure inside the test limit returns 429.
- Pausing one worker makes its claim return 204. A second worker, stored as `platform=win32`, receives the job and is shown `online-busy`. The paused worker is `paused`, not offline.
- A concurrency cap above the worker's registered parallelism is 400.
- Graceful stop leaves the leased mock job runnable, refuses a second claim, and after that job completes exports a training-winner `gatk.conf` with `research_only`.
- Stop with a held-out dataset ends `STOPPED` and export returns 409 `not_validated`.
- A failed job keeps `official_score` null, returns the bounded log excerpt, and an idempotent retry does not bump the attempt twice.
- A heartbeat older than the stale window is `offline`. A fresh heartbeat with the wrong Minos commit is `incompatible`.
- `objective` null is omitted from the best-so-far series; objective `0` is kept.
- OpenAPI contains the new overview, login, drain, stop, metrics, logs, and export paths.
- `start: false` creates `DRAFT` with no jobs. `POST /start` then allows a claim.

The older scheduler tests still pass, including tell-once, stale lease rejection, and the 10-trial mock study.

## Browser pass on this host

A coordinator was started on `127.0.0.1:8091` with loopback admin bypass turned off. In the browser: login with the PBKDF2 password reached Overview; the draft study showed `DRAFT`, `0/1` told trials, and **Not validated**; Start changed it to `RUNNING`; Pause changed it to `PAUSED` and revealed Resume; Cancel stayed disabled; Datasets listed `train-a` with its manifest hash and no file path; Workers said none had registered; the created job was `PENDING` with a blank score rather than zero; a reload stayed signed in on the same job.

## Not verified here

- A physical Windows 11 worker and Docker Desktop.
- Playwright.
- TLS termination. The nginx file is an example.
- A second official GATK study driven from the new buttons. One official practice evaluation was already recorded for the tuner (`official_score=95.32677496066316`, v1, commit `4b3c0944…`) before this dashboard work. That number is not a dashboard demo and is not a v2 score.
