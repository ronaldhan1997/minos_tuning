# Runbook

## Air-gapped bring-up

On a machine that has internet, once:

1. Clone `minos_subnet` and check out `4b3c0944b517d09be9b54eb8fa3158ae700b5d5f`.
2. Create the venv and `pip install -e "./tuning/minos_tuning/minos-lan-tuner[postgres]"`.
3. `pip download -d wheels -r` the same set if the coordinator itself will be offline later.
4. `docker pull` the three images named in `docs/minos_integration.md`. `docker save` them to a tar and `docker load` on every worker.
5. Download authorized practice samples with the Minos miner practice flow while the platform is reachable. Copy `datasets/practice` and `datasets/reference` onto the coordinator disk. Workers do not need the files in advance; they fetch them from the coordinator.

After that, studies run with no outbound network. Container runs use whatever network the official template leaves in place (the default bridge). They do not need DNS if the images are already loaded.

## Start

```bash
docker compose -f tuning/minos_tuning/minos-lan-tuner/deployment/docker-compose.yml up -d
export DATABASE_URL=postgresql+psycopg://minos:minos@127.0.0.1:5432/minos_tuner
export COORDINATOR_ADMIN_TOKEN="$(openssl rand -hex 24)"
cd tuning/minos_tuning/minos-lan-tuner && alembic upgrade head
minosctl serve --host 127.0.0.1 --port 8080
```

SQLite is enough for a single-user trial: `DATABASE_URL=sqlite:////var/lib/minos-tuner/tuner.sqlite3`. Optuna then uses `tuner.sqlite3.optuna` beside it. Postgres keeps Optuna tables in the same database.

## Worker dies

The lease expires (`job timeout + LEASE_GRACE_SECONDS`, default 120s). If attempts remain, the job returns to `PENDING` and another worker can claim it. The old lease token is rejected with HTTP 409. A completed trial is told to Optuna once. Restarting the coordinator reloads the study and finishes any trial whose jobs are already terminal but whose `tell` did not commit.

## Coordinator dies

Start `minosctl serve` again on the same `DATABASE_URL`. `reconcile_all` expires leases, adopts Optuna trials that were asked but had no row, and tells trials whose jobs already finished. It does not call `tell` on a trial Optuna has already finished.

## Bad score or failed GATK

`minosctl experiment status <name>` and the dashboard list `error_class`. `invalid_config`, `not_recorded`, and `scoring_unavailable` are not retried. `gatk_failed` and `timeout` retry until `max_attempts`. None of these become a zero objective.

`not_recorded` means AdvancedScorer returned a number the validator would drop (out of range, or the zero-input fingerprint around combined_final 0.25). The raw number is on the job details as `discarded_official_score`.

## Version mismatch

A worker whose Minos commit, scorer name, or GATK image id differs from the study is not offered official jobs. `minos-worker doctor` prints the local image id and commit.

## Backup

Stop the coordinator. Copy the Postgres volume or both SQLite files. Start the coordinator. Practice BAMs are not inside the database; back up the import paths separately if you need them.

## Logs

The coordinator uses Python logging (`minos.coordinator`). Workers use `minos.worker` and the level in `LOG_LEVEL`. Uvicorn writes access logs. On Ubuntu, journald keeps the systemd unit's stdout. Rotate with `journald` rate limits or a `logrotate` snippet on any file handler you add. The process does not ship logs off the host.
