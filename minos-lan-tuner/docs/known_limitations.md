# Known limitations

- The dashboard cannot power on a laptop, start Docker Desktop, or start `minos-worker`. An offline worker stays offline until that machine sends a heartbeat.
- Nothing in this UI submits a config to a live Minos round.
- Cancel active jobs is disabled in the browser. `POST /experiments/{id}/cancel` still exists for the CLI. The worker does not read `cancelled_job_ids`, so a cancelled lease does not stop the container. Pause and stop scheduling are the supported controls: they stop new assignments, and a leased GATK run finishes.
- Windows 11 with Docker Desktop was not executed in this environment. The UI shows `platform=win32` when a worker reports it, and the runtime note says Linux containers need Docker Desktop/WSL2. It does not claim a WSL2 session was observed.
- Mock scores are labeled mock. They are not AdvancedScorer results. v1 and v2 are not compared as if they were the same scale.
- One coordinator process owns Optuna. Do not run two `minosctl serve` processes on the same database.
- A search profile can still sample `min_assembly_region_size` above `max_assembly_region_size` when stage 2 is used. The official validator does not compare those two keys, and the trial fails instead of being scored as zero.
- Parameter importance is whatever Optuna returns for that study. Fewer than four completed trials reports that the chart is unavailable.
- The ETA is a mean of completed job durations times remaining jobs. It is omitted until three jobs have elapsed times, and it is not a prediction of the score.
- Playwright is not installed here, so the browser pass below was done against the running coordinator rather than a Playwright script.
- `ruff` and `mypy` are configured in `pyproject.toml` and were not executed in this environment because those tools are not installed.
