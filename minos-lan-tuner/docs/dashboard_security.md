# Dashboard security

This is a LAN control plane for an already trusted coordinator. It does not add cloud login, and it does not submit to a live Minos round.

## Authentication

- Browser passwords are checked against `DASHBOARD_ADMIN_PASSWORD_HASH` (PBKDF2-HMAC-SHA256, 200000 iterations). The plaintext password is not stored. Generate the hash with `minosctl auth hash-password` and then unset `DASHBOARD_ADMIN_PASSWORD`.
- The session cookie `minos_session` is HMAC-signed, HttpOnly, SameSite=Lax, and expires after `DASHBOARD_SESSION_SECONDS` (default 12 hours).
- Cookie-authenticated POST, PATCH, and DELETE must send `X-CSRF-Token`. GET does not. Bearer admin calls are not cookie flows and do not use CSRF.
- Login failures are limited per client address (`DASHBOARD_LOGIN_LIMIT`, default 8, window 5 minutes). The process keeps that counter in memory.
- `COORDINATOR_ADMIN_TOKEN` still authorizes the CLI. Treat it as a secret. Loopback bypass remains available for local `minosctl` when `MINOS_ALLOW_LOOPBACK_ADMIN` is not `0`. A browser on another machine is not loopback.

## What the browser cannot do

- It cannot select a Docker image, pass a shell command, or choose an output path.
- Dataset practice import rejects paths outside the configured roots.
- Job and export downloads do not take a filesystem path. Logs are the bounded excerpt the worker already uploaded on failure.
- Provenance and manifest views omit local paths.
- Worker enable, pause, drain, and the concurrency cap only change scheduler assignment.
- Cancel-active-jobs is not wired in the UI because the worker does not stop the GATK container.

## Network

Default bind is `127.0.0.1`. Binding a LAN address without TLS means anyone on that LAN who can reach the port can attempt login. That is called out on the login page and in `/api/v1/system/status`. Put nginx or Caddy in front with a certificate the laptops trust, and set `DASHBOARD_COOKIE_SECURE=1`.

PostgreSQL stays on localhost. Workers never receive the database URL.

Audit rows record the actor and the action. They do not record the password or the admin token.
