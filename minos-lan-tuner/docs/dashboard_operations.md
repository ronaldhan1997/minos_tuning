# Dashboard operations

The dashboard is the compiled files in `apps/dashboard/dist`. `minosctl serve` serves them on the same port as the API. Rebuild after a UI change:

```bash
# Node 20 or newer. The system Node 12 cannot build this app.
cd tuning/minos_tuning/minos-lan-tuner/apps/dashboard
npm install
npm run build
```

## One-time password

```bash
cd tuning/minos_tuning/minos-lan-tuner
export DASHBOARD_ADMIN_PASSWORD='choose-a-long-password'
python -m apps.coordinator.cli auth hash-password
unset DASHBOARD_ADMIN_PASSWORD
```

Put the printed `pbkdf2_sha256$...` line in `DASHBOARD_ADMIN_PASSWORD_HASH`. Set `DASHBOARD_ADMIN_USER` if you do not want `admin`. Set `DASHBOARD_SESSION_SECRET` to a long random string so sessions survive a token rotation; if you leave it empty, sessions are tied to `COORDINATOR_ADMIN_TOKEN`.

## Start the coordinator

```bash
cd tuning/minos_tuning/minos-lan-tuner
export DATABASE_URL=sqlite:///./tuner.sqlite3
export COORDINATOR_ADMIN_TOKEN='the-same-token-minosctl-uses'
export DASHBOARD_ADMIN_USER=admin
export DASHBOARD_ADMIN_PASSWORD_HASH='pbkdf2_sha256$...'
alembic upgrade head
minosctl serve --host 127.0.0.1 --port 8080
```

`127.0.0.1` is the safe default. On a trusted LAN, bind the Ethernet address explicitly, for example `--host 192.168.1.10`. Do not publish PostgreSQL. The compose file binds Postgres to `127.0.0.1:5432` only.

Open `http://<coordinator>:8080/` from a browser on that network. Sign in. The API and the pages are the same origin, so the session cookie is not sent anywhere else.

## Use the study

1. Import a practice sample on the coordinator (`minosctl dataset import-practice` or the Datasets page). The page only accepts paths under the repo `datasets/` directory or `MINOS_DATASET_ROOTS`.
2. Create an experiment. Pick datasets, scoring version, mock or official, and a search profile from `config/search_space.yaml`. Leave "Start scheduling immediately" off to keep a `DRAFT`.
3. Start workers with `minos-worker start` on each Ubuntu or Windows machine. They appear on Workers. The server cannot start those processes.
4. Pause an experiment to stop new leases. A GATK run that already started keeps going.
5. Drain a worker to stop new leases for that machine only. Enable it when it should take work again.
6. Stop scheduling when the budget should end early. Held-out studies that have not finished validation cannot be exported.
7. After `COMPLETED`, export `gatk.conf` and the provenance JSON. If the study had no held-out datasets, the page says the file is a training winner.

`/legacy` is the previous no-JavaScript status table. It requires the same admin credential.

## Firewall

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw deny 5432
```

Use `deployment/dashboard.nginx.conf` when you terminate TLS. Set `DASHBOARD_COOKIE_SECURE=1` only after the browser is speaking HTTPS to the proxy.

## Backup

The coordinator database and the Optuna sidecar (SQLite file plus `<file>.optuna`, or the Postgres database) are the study record. Copy them while the process is stopped, or use Postgres `pg_dump`. Dataset bytes stay in the original import paths; the database stores those paths and the checksums, not a second copy of the BAMs.
