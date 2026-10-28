# Security

The coordinator is a LAN service. It is not a public submission endpoint.

- Bind `127.0.0.1` unless every client is on a trusted Ethernet or Wi-Fi segment. `minosctl serve --host 0.0.0.0` prints a warning. Do not add a WAN port forward.
- Worker tokens are random, shown once, and stored as SHA-256. The admin token lives in `COORDINATOR_ADMIN_TOKEN` and is compared with `hmac.compare_digest`.
- This development server speaks HTTP. On a LAN you do not fully trust, terminate TLS in a reverse proxy and leave PostgreSQL unpublished. Compose publishes Postgres on `127.0.0.1` only.
- Loopback clients may call admin routes without a bearer token while `MINOS_ALLOW_LOOPBACK_ADMIN=1` (the default). Set it to `0` to require the token even from localhost. The dashboard does not print tokens, worker-token hashes, or dataset filesystem paths.
- Workers never receive a database URL. Dataset bytes require a worker or admin bearer token (loopback admin bypass included).
- Jobs carry a server-built GATK config. The worker runs `templates.gatk.variant_call` and rejects a job whose image name, image id, Minos commit, or scoring version disagrees with the pinned checkout. There is no field for a shell command, an image override, or a host path.
- Logs should stay on dataset ids, job ids, and error classes. hap.py stderr can still contain container paths; keep coordinator logs on the trusted host.
- Practice BAMs and truth VCFs are licensed to you by whoever gave you the Minos practice set. The tuner stores local paths and serves bytes on the LAN. It does not upload them.
- Back up the Postgres data directory, or both SQLite files (`tuner.sqlite3` and `tuner.sqlite3.optuna`). Restore by stopping the coordinator, replacing those files, and starting again. Tokens in the backup are hashes; the cleartext worker tokens are only on the workers.
