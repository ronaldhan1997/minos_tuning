# Minos LAN GATK tuner

Distributed, LAN-only hyperparameter search for GATK HaplotypeCaller. An Ubuntu coordinator owns Optuna and PostgreSQL. Ubuntu and Windows workers claim one whole trial evaluation at a time (GATK, then the official hap.py / AdvancedScorer path) and report the score. Nothing is submitted to a live Minos round.

```text
Windows or Ubuntu browser / minosctl
          |
       Ubuntu coordinator
   FastAPI + Optuna + PostgreSQL
   scheduler + dataset registry
       /            |            \
Ubuntu worker   Windows worker   Ubuntu or Windows worker
Python agent    Python agent     Python agent
Docker GATK     Docker Desktop   Docker GATK
Minos scorer    WSL2 Linux       Minos scorer
SSD cache       SSD cache        SSD cache
```

Pinned Minos commit: `4b3c0944b517d09be9b54eb8fa3158ae700b5d5f`. Interfaces, and the things that were not verified, are in [docs/minos_integration.md](docs/minos_integration.md).

Mock mode is a deterministic stand-in for tests and for wiring the cluster. A mock number is not an AdvancedScorer score. The API labels it `score_source=mock`.

## Install

On the Ubuntu coordinator, from the `minos_subnet` checkout:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e "./tuning/minos_tuning/minos-lan-tuner[postgres]"
```

Workers that only use SQLite-free HTTP can install without the Postgres extra:

```bash
pip install -e ./tuning/minos_tuning/minos-lan-tuner
```

`MINOS_REPO_ROOT` defaults to the Minos checkout that contains `start-miner.sh`. Set it if the tuner is installed somewhere else.

Pull the images named in the integration doc before an official study. Workers do not need internet during a study if those images are already loaded and the datasets are on the coordinator.

## Coordinator

```bash
cp tuning/minos_tuning/minos-lan-tuner/.env.example tuning/minos_tuning/minos-lan-tuner/.env
# set COORDINATOR_ADMIN_TOKEN
set -a && . tuning/minos_tuning/minos-lan-tuner/.env && set +a
cd tuning/minos_tuning/minos-lan-tuner
alembic upgrade head
minosctl serve --host 127.0.0.1 --port 8080
```

Postgres instead of SQLite:

```bash
docker compose -f tuning/minos_tuning/minos-lan-tuner/deployment/docker-compose.yml up -d
export DATABASE_URL=postgresql+psycopg://minos:minos@127.0.0.1:5432/minos_tuner
export COORDINATOR_ADMIN_TOKEN="$(openssl rand -hex 24)"
cd tuning/minos_tuning/minos-lan-tuner && alembic upgrade head
minosctl serve --host 127.0.0.1 --port 8080
```

`minosctl serve` warns that the socket is HTTP. Use a reverse proxy with TLS if the LAN is not fully trusted. See [docs/security.md](docs/security.md).

OpenAPI is `http://127.0.0.1:8080/openapi.json`. Health is `/healthz` and `/readyz`.

## Dashboard

The browser control plane is served by the same process at `http://<coordinator>:8080/`. Build it once with Node 20 (`apps/dashboard`: `npm install && npm run build`). The checked-in `apps/dashboard/dist` is what `minosctl serve` serves.

```bash
export DASHBOARD_ADMIN_PASSWORD='choose-a-long-password'
python -m apps.coordinator.cli auth hash-password
unset DASHBOARD_ADMIN_PASSWORD
export DASHBOARD_ADMIN_PASSWORD_HASH='paste-the-pbkdf2-line'
```

Sign in from a laptop on the LAN. Pause and stop scheduling change the coordinator only; they do not start or stop worker operating systems. Cancel-active-jobs is disabled because the worker does not kill a running GATK container. Export stays blocked until the study completes, and a study that stopped before held-out verification cannot be exported.

Details: [docs/dashboard_operations.md](docs/dashboard_operations.md), [docs/dashboard_api.md](docs/dashboard_api.md), [docs/dashboard_security.md](docs/dashboard_security.md), [docs/known_limitations.md](docs/known_limitations.md).

## Datasets

Import runs on the coordinator host, because it hashes files that are already on that disk. It does not upload them.

```bash
cd tuning/minos_tuning/minos-lan-tuner
export DATABASE_URL=sqlite:///./tuner.sqlite3   # or the Postgres URL
export COORDINATOR_ADMIN_TOKEN=...              # same value the server uses
# Scoring version is required. This process does not guess the live network's version.
minosctl dataset import-practice \
  --sample ../datasets/practice/round_<id>/sample.json \
  --scoring-version v1
minosctl dataset list
```

A hand-written manifest works too: `minosctl dataset import --manifest ./dataset_manifest.json`. Roles and checksums are described in the integration doc. A second import of the same dataset id is accepted only when the manifest hash matches.

## Study

`--scoring-version` is required even though a shorter example would be easier to type. v1 and v2 are different scales. The study will not start without an explicit choice, and it will not mix them.

```bash
minosctl worker token worker-a
# prints the token once

minosctl experiment create \
  --name gatk-v2 \
  --search-space config/search_space.yaml \
  --profile stage1 \
  --trials 100 \
  --scoring-version v1 \
  --train <training-dataset-id> \
  --holdout <held-out-dataset-id> \
  --execution-mode official

minosctl experiment status gatk-v2
minosctl experiment export gatk-v2 --output exported/gatk.conf
```

`--execution-mode mock` runs the same scheduler with stand-in numbers. Use it to test workers before spending GATK time.

The budget counts Optuna trials, including the enqueued baseline (official defaults from `GATK_QUALITY_PARAMS`). The objective is the mean training score, minus `variability_penalty` times the population standard deviation when there are at least two training datasets and the penalty is non-zero. Held-out scores are not sent to Optuna.

Winner rule: highest mean held-out score among the top N completed training configs, plus the baseline when it completed and was not already in that set. Ties break toward the higher training objective, then the smaller config SHA-256. With no held-out datasets, the highest training objective wins. Failed jobs are not zeros. The rule is copied into the exported conf header and into `gatk.provenance.json`.

TPE is seeded (`--seed`, default 1). A multi-worker run is not suggestion-reproducible, because the sampler sees results in completion order.

Pause, resume, and cancel:

```bash
minosctl experiment pause gatk-v2
minosctl experiment resume gatk-v2
minosctl experiment cancel gatk-v2
```

From a laptop, set `COORDINATOR_URL` and `COORDINATOR_ADMIN_TOKEN` and the same `experiment status|pause|resume|cancel|export` commands talk HTTP. `dataset import` still has to run on the coordinator.

## Workers

Ubuntu and Windows share this agent. On Ubuntu, run `deployment/ubuntu/dist/MinosClient` in a terminal. It asks for the server address and token, then stays connected until Ctrl+C. On Windows, copy `deployment/windows/dist/MinosClient.exe` and double-click it. Windows prerequisites are in [docs/windows.md](docs/windows.md).

```bash
cp config/worker.yaml.example /etc/minos-worker.yaml
# edit coordinator_url, worker_id, cache_dir, work_dir, scoring_version, execution_mode
export WORKER_TOKEN='the token from minosctl worker token'
export COORDINATOR_URL=http://192.168.1.10:8080
export WORKER_ID=worker-a
export SCORING_VERSION=v1
minos-worker doctor --config /etc/minos-worker.yaml
minos-worker start --config /etc/minos-worker.yaml
```

`doctor` checks Python, the pinned commit, Docker, the three images, and writable directories. `windows_hardware` stays failed on purpose until someone runs this on Windows 11. It does not fail the command.

`minos-worker stop` writes `worker.stop` and signals the pid. The agent sets a stop flag that `reap_live_containers` uses to remove the GATK container the official template started.

Ubuntu persistence: `deployment/systemd/minos-worker.service`. Windows: `deployment/windows/register-worker-task.ps1` (not executed in this build).

Each worker keeps a content-addressed cache under `CACHE_DIR`. Downloads retry from a `.partial` file. Results that could not be uploaded sit in `work_dir/outbox` until the next loop.

## Tests

From `tuning/minos_tuning/minos-lan-tuner`, with the venv that has the tuner dependencies:

```bash
python -m pytest -m "not real_minos"
```

That run uses SQLite, mock execution, and an in-process worker. It covers the search-space contract, the v1/v2 split, Windows path conversion, leases, a stale completion, coordinator restart, a 10-trial / 2-dataset mock study (20 training jobs) across two workers, held-out selection, and export.

An official GATK + hap.py run is opt-in and is not part of the default suite:

```bash
MINOS_REAL=1 \
MINOS_SCORING_VERSION=v1 \
MINOS_PRACTICE_SAMPLE=/absolute/path/sample.json \
python -m pytest tests/test_real_minos.py -s
```

Use `v1` or `v2` on purpose. The test prints the score it actually computed. That number is one run on one sample, not a benchmark, and it does not say which version the live network is using.

On this Ubuntu host, that command was run once with `MINOS_SCORING_VERSION=v1`, 4 GATK threads, 8 GB, and `datasets/practice/round_d17838295b8cfe54/sample.json` (`chr21:19858319-21637960`). AdvancedScorer v1 returned **95.32677496066316**. Commit `4b3c0944b517d09be9b54eb8fa3158ae700b5d5f`. The local GATK image id at the time was `sha256:8a29403b528a417cd1630c33affe82726019788e24e4dfe00e2586c6b4195e9f`. This is one observation, not a tuned result and not a comparison against the live network.

## Layout

```text
apps/coordinator    FastAPI, scheduler, Optuna ask/tell, CLI (minosctl)
apps/worker         register / start / doctor / stop
shared              HTTP bodies, hashes, Windows mount helper
integrations/minos  pinned GATK + AdvancedScorer adapter, mock engine
datasets            manifest import (no BAM files)
database            SQLAlchemy models, Alembic
config              search_space.yaml, worker example
deployment          Compose, systemd, Windows task script
```

## Known limits

- Windows 11 Docker Desktop was not executed here.
- The pinned GATK command has CPU, memory, and timeout limits, and read-only input mounts. It does not set `--pids-limit` or `--network=none`. Those flags are not added outside the template.
- One coordinator process. Run a single `minosctl serve`.
- SQLite stores Optuna in a sidecar file (`<db>.optuna`) so the scheduler transaction and Optuna can both write. Postgres uses one database.
- `min_assembly_region_size` can be sampled above `max_assembly_region_size`. The official validator allows that pair. GATK then fails the trial.
- The tuner does not read miner hotkeys and does not upload BAMs.
