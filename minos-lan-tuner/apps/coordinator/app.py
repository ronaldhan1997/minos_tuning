"""HTTP API. PostgreSQL stays behind this process."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from apps.coordinator.auth import SessionAuth
from apps.coordinator.dashboard import render_dashboard
from apps.coordinator.dashboard_http import _idem, register_dashboard_routes, require_admin
from apps.coordinator.errors import ServiceError
from apps.coordinator.service import Coordinator
from apps.coordinator.ui import install_ui
from database.session import init_engine
from shared.schemas import (
    ClaimRequest,
    CompleteRequest,
    DatasetImport,
    ExperimentCreate,
    FailRequest,
    Heartbeat,
    ReleaseRequest,
    TokenIssue,
    WorkerRegister,
)


def create_app(database_url: str, admin_token: str, lease_grace_seconds: int = 120) -> FastAPI:
    logging.getLogger("optuna").setLevel(logging.WARNING)
    init_engine(database_url)
    app = FastAPI(
        title="Minos LAN GATK tuner",
        version="0.1.0",
        description=(
            "LAN coordinator for Optuna-tuned GATK HaplotypeCaller evaluations. "
            "Mock scores are not Minos AdvancedScorer results. "
            "This service does not submit to live rounds."
        ),
    )
    app.state.coordinator = Coordinator(admin_token, lease_grace_seconds)
    app.state.coordinator.reconcile_all()

    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message})

    @app.get("/healthz")
    @app.get("/api/v1/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    @app.get("/api/v1/readyz")
    def readyz(request: Request) -> dict[str, str]:
        request.app.state.coordinator.ready()
        return {"status": "ready"}

    @app.get("/legacy", response_class=HTMLResponse)
    def legacy_dashboard(request: Request) -> str:
        require_admin(request)
        return render_dashboard(request.app.state.coordinator.dashboard())

    @app.post("/api/v1/workers/tokens", status_code=201)
    def issue_token(body: TokenIssue, request: Request) -> dict[str, str]:
        actor = require_admin(request)
        token = _coord(request).issue_token(body.worker_id, actor=actor)
        return {"worker_id": body.worker_id, "token": token}

    @app.get("/api/v1/workers/config")
    def worker_config(request: Request) -> dict[str, object]:
        return _coord(request).worker_config(_bearer_required(request))

    @app.post("/api/v1/workers/register")
    def register(body: WorkerRegister, request: Request) -> dict[str, object]:
        return _coord(request).register(body, _bearer_required(request))

    @app.post("/api/v1/workers/heartbeat")
    def heartbeat(body: Heartbeat, request: Request) -> dict[str, object]:
        return _coord(request).heartbeat(_bearer_required(request), body)

    @app.post("/api/v1/jobs/claim")
    def claim(body: ClaimRequest, request: Request, wait_seconds: int = 0) -> Response:
        payload = _coord(request).claim(_bearer_required(request), body, wait_seconds=max(0, min(wait_seconds, 30)))
        if payload is None:
            return Response(status_code=204)
        return JSONResponse(payload)

    @app.post("/api/v1/jobs/{job_id}/complete")
    def complete(job_id: str, body: CompleteRequest, request: Request) -> dict[str, object]:
        return _coord(request).complete(_bearer_required(request), job_id, body)

    @app.post("/api/v1/jobs/{job_id}/fail")
    def fail(job_id: str, body: FailRequest, request: Request) -> dict[str, object]:
        return _coord(request).fail(_bearer_required(request), job_id, body)

    @app.post("/api/v1/jobs/{job_id}/release")
    def release(job_id: str, body: ReleaseRequest, request: Request) -> dict[str, object]:
        return _coord(request).release(_bearer_required(request), job_id, body)

    @app.get("/api/v1/datasets")
    def list_datasets(request: Request) -> list[dict[str, object]]:
        _admin_or_worker(request)
        return _coord(request).list_datasets()

    @app.post("/api/v1/datasets/import", status_code=201)
    def import_dataset(body: DatasetImport, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _import_audited(request, body.manifest, actor)

    @app.delete("/api/v1/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _coord(request).delete_dataset(dataset_id)

    @app.get("/api/v1/datasets/{dataset_id}/manifest")
    def manifest(dataset_id: str, request: Request) -> dict[str, object]:
        _admin_or_worker(request)
        return _coord(request).manifest(dataset_id)

    @app.get("/api/v1/datasets/{dataset_id}/blobs/{sha256}")
    def blob(dataset_id: str, sha256: str, request: Request) -> Response:
        _admin_or_worker(request)
        path, size = _coord(request).blob(dataset_id, sha256)
        return _file_bytes(path, size, sha256, request.headers.get("range"))

    @app.post("/api/v1/experiments", status_code=201)
    def create_experiment(body: ExperimentCreate, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).create_experiment(body, actor=actor, idempotency_key=_idem(request))

    @app.get("/api/v1/experiments")
    def list_experiments(
        request: Request,
        limit: int | None = None,
        cursor: str | None = None,
        q: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]] | dict[str, object]:
        require_admin(request)
        rows = _coord(request).list_experiments()
        if limit is None and cursor is None and not q and not status:
            return rows
        from apps.coordinator.present import decode_cursor, encode_cursor

        if q:
            needle = q.lower()
            rows = [row for row in rows if needle in str(row["name"]).lower()]
        if status:
            rows = [row for row in rows if row["status"] == status]
        rows = sorted(rows, key=lambda row: (str(row["created_at"]), str(row["id"])), reverse=True)
        if cursor:
            created, row_id = decode_cursor(cursor)
            rows = [row for row in rows if (str(row["created_at"]), str(row["id"])) < (created, row_id)]
        page_limit = max(1, min(limit or 50, 200))
        page = rows[:page_limit]
        next_cursor = None
        if len(rows) > page_limit:
            last = page[-1]
            next_cursor = encode_cursor(str(last["created_at"]), str(last["id"]))
        return {"items": page, "next_cursor": next_cursor}

    @app.get("/api/v1/experiments/{key}")
    def get_experiment(key: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _coord(request).get_experiment(key)

    @app.post("/api/v1/experiments/{key}/pause")
    def pause(key: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).pause(key, actor=actor, idempotency_key=_idem(request))

    @app.post("/api/v1/experiments/{key}/resume")
    def resume(key: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).resume(key, actor=actor, idempotency_key=_idem(request))

    @app.post("/api/v1/experiments/{key}/cancel")
    def cancel(key: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).cancel(key, actor=actor, idempotency_key=_idem(request))

    @app.get("/api/v1/experiments/{key}/leaderboard")
    def leaderboard(key: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _coord(request).leaderboard(key)

    @app.get("/api/v1/experiments/{key}/trials")
    def trials(key: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _coord(request).trial_rows(key)

    @app.get("/api/v1/experiments/{key}/export")
    def export(key: str, request: Request) -> PlainTextResponse:
        require_admin(request)
        return PlainTextResponse(_coord(request).export_conf(key))

    app.state.sessions = SessionAuth.from_env(admin_token)
    register_dashboard_routes(app)
    install_ui(app)
    return app


def app_from_env() -> FastAPI:
    url = os.environ["DATABASE_URL"]
    token = os.environ["COORDINATOR_ADMIN_TOKEN"]
    grace = int(os.environ.get("LEASE_GRACE_SECONDS", "120"))
    return create_app(url, token, grace)


def _coord(request: Request) -> Coordinator:
    return request.app.state.coordinator


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header.split(" ", 1)[1].strip()
    return token or None


def _bearer_required(request: Request) -> str:
    token = _bearer(request)
    if token is None:
        raise ServiceError(401, "unauthorized", "missing bearer token")
    return token


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1"}


def _import_audited(request: Request, manifest: dict, actor: str) -> dict[str, object]:
    from apps.coordinator.service import _audit

    view = _coord(request).import_document(manifest)
    with _coord(request)._db() as db:
        _audit(
            db,
            actor=actor,
            action="dataset.import",
            target_type="dataset",
            target_id=str(view.get("dataset_id")),
            result="ok",
            detail={},
        )
    return view


def _admin(request: Request) -> None:
    require_admin(request)


def _admin_or_worker(request: Request) -> None:
    token = _bearer(request)
    try:
        _coord(request).require_admin_token(token, loopback=_loopback(request))
        return
    except ServiceError:
        if token is None:
            raise
    _coord(request).worker_from_token(token)


def _file_bytes(path: Path, size: int, sha256: str, range_header: str | None) -> Response:
    headers = {"Accept-Ranges": "bytes", "ETag": sha256}
    if not range_header:
        def chunks():
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    yield block

        headers["Content-Length"] = str(size)
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers=headers)
    if not range_header.startswith("bytes="):
        raise ServiceError(416, "range", "only bytes ranges are supported")
    spec = range_header[len("bytes=") :].split(",", 1)[0].strip()
    start_text, sep, end_text = spec.partition("-")
    if not sep:
        raise ServiceError(416, "range", "invalid range")
    try:
        start = int(start_text) if start_text else 0
        end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise ServiceError(416, "range", "invalid range") from exc
    if start < 0 or end < start or start >= size:
        raise ServiceError(416, "range", "range is outside the file")
    end = min(end, size - 1)
    length = end - start + 1

    def ranged():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(ranged(), status_code=206, media_type="application/octet-stream", headers=headers)
