"""Browser and operator routes. Worker claim/complete routes stay in app.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from apps.coordinator.auth import SessionAuth
from apps.coordinator.errors import ServiceError
from apps.coordinator.reads import DashboardReads
from apps.coordinator.service import Coordinator, _audit
from integrations.minos.pin import repo_root
from shared.schemas import LoginRequest, PracticeImport, SampleImport, WorkerPolicyPatch


def register_dashboard_routes(app: FastAPI) -> None:
    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest, request: Request) -> JSONResponse:
        sessions: SessionAuth = request.app.state.sessions
        ip = request.client.host if request.client else "unknown"
        if not sessions.configured:
            raise ServiceError(
                503,
                "auth_not_configured",
                "Set DASHBOARD_ADMIN_PASSWORD_HASH before using browser login.",
            )
        if sessions.too_many(ip):
            raise ServiceError(429, "rate_limited", "Too many login attempts. Wait and try again.")
        if not sessions.check_password(body.username, body.password):
            sessions.record_failure(ip)
            _record(request, "login.fail", "session", "dashboard", "denied", {"actor": body.username})
            raise ServiceError(401, "unauthorized", "username or password was rejected")
        sessions.clear_failures(ip)
        token, csrf = sessions.issue(body.username)
        _record(request, "login.ok", "session", "dashboard", "ok", {"actor": body.username})
        response = JSONResponse({"authenticated": True, "username": body.username, "csrf": csrf, "role": "admin"})
        _cookies(response, token, csrf)
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request) -> Response:
        require_admin(request)
        response = Response(status_code=204)
        response.delete_cookie("minos_session", path="/")
        response.delete_cookie("minos_csrf", path="/")
        return response

    @app.get("/api/v1/auth/session")
    def session(request: Request) -> dict[str, object]:
        payload = _session_payload(request)
        if payload is None:
            return {"authenticated": False, "username": None, "csrf": None, "role": None}
        return {"authenticated": True, "username": payload["u"], "csrf": payload["csrf"], "role": "admin"}

    @app.get("/api/v1/system/status")
    def system_status(request: Request) -> dict[str, object]:
        require_admin(request)
        status = _reads(request).system_status()
        status["dashboard_login_configured"] = request.app.state.sessions.configured
        return status

    @app.get("/api/v1/overview")
    def overview(request: Request) -> dict[str, object]:
        require_admin(request)
        return _reads(request).overview()

    @app.get("/api/v1/workers/tokens")
    def worker_tokens(request: Request) -> dict[str, object]:
        require_admin(request)
        return _coord(request).list_tokens()

    @app.post("/api/v1/workers/{worker_id}/revoke-token")
    def revoke_token(worker_id: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).revoke_token(worker_id, actor=actor)

    @app.get("/api/v1/workers")
    def workers(
        request: Request,
        limit: int = 50,
        cursor: str | None = None,
        q: str | None = None,
    ) -> dict[str, object]:
        require_admin(request)
        return _reads(request).workers(limit=limit, cursor=cursor, q=q)

    @app.get("/api/v1/workers/{worker_id}")
    def worker(worker_id: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _reads(request).worker(worker_id)

    @app.post("/api/v1/workers/{worker_id}/enable")
    def enable(worker_id: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).set_scheduling(worker_id, "enabled", actor=actor, idempotency_key=_idem(request))

    @app.post("/api/v1/workers/{worker_id}/pause")
    def pause_worker(worker_id: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).set_scheduling(worker_id, "paused", actor=actor, idempotency_key=_idem(request))

    @app.post("/api/v1/workers/{worker_id}/drain")
    def drain_worker(worker_id: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).set_scheduling(worker_id, "draining", actor=actor, idempotency_key=_idem(request))

    @app.patch("/api/v1/workers/{worker_id}/policy")
    def policy(worker_id: str, body: WorkerPolicyPatch, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).set_policy(
            worker_id,
            max_concurrent_jobs=body.max_concurrent_jobs,
            reset=body.reset,
            actor=actor,
            idempotency_key=_idem(request),
        )

    @app.get("/api/v1/jobs")
    def jobs(
        request: Request,
        limit: int = 50,
        cursor: str | None = None,
        experiment_id: str | None = None,
        worker_id: str | None = None,
        dataset_id: str | None = None,
        state: str | None = None,
        trial_id: str | None = None,
        q: str | None = None,
    ) -> dict[str, object]:
        require_admin(request)
        return _reads(request).jobs(
            limit=limit,
            cursor=cursor,
            experiment_id=experiment_id,
            worker_id=worker_id,
            dataset_id=dataset_id,
            state=state,
            trial_id=trial_id,
            q=q,
        )

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _reads(request).job(job_id)

    @app.get("/api/v1/jobs/{job_id}/logs")
    def job_logs(job_id: str, request: Request, tail: int = 4000) -> dict[str, object]:
        require_admin(request)
        return _reads(request).job_log(job_id, tail)

    @app.post("/api/v1/jobs/{job_id}/retry")
    def retry(job_id: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).retry_job(job_id, actor=actor, idempotency_key=_idem(request))

    @app.get("/api/v1/search-space")
    def search_space(request: Request) -> dict[str, object]:
        require_admin(request)
        return _reads(request).search_space()

    @app.post("/api/v1/experiments/{key}/start")
    def start(key: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).start(key, actor=actor, idempotency_key=_idem(request))

    @app.post("/api/v1/experiments/{key}/stop")
    def stop(key: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).stop(key, actor=actor, idempotency_key=_idem(request))

    @app.get("/api/v1/experiments/{key}/metrics")
    def metrics(key: str, request: Request, include_importance: bool = False) -> dict[str, object]:
        require_admin(request)
        return _reads(request).metrics(key, include_importance=include_importance)

    @app.get("/api/v1/experiments/{key}/log")
    def experiment_log(key: str, request: Request) -> dict[str, object]:
        require_admin(request)
        return _reads(request).experiment_log(key)

    @app.get("/api/v1/experiments/{key}/events")
    def events(key: str, request: Request, limit: int = 50) -> dict[str, object]:
        require_admin(request)
        return _reads(request).events(key, limit)

    @app.post("/api/v1/experiments/{key}/export")
    def export_post(key: str, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        return _coord(request).create_export(key, actor=actor, idempotency_key=_idem(request))

    @app.get("/api/v1/exports/{export_id}/download")
    def download(export_id: str, request: Request, part: str = "conf") -> PlainTextResponse:
        require_admin(request)
        body, media, filename = _coord(request).export_record(export_id, part)
        return PlainTextResponse(
            body,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v1/audit")
    def audit(request: Request, limit: int = 50, cursor: str | None = None) -> dict[str, object]:
        require_admin(request)
        return _reads(request).audit(limit=limit, cursor=cursor)

    @app.post("/api/v1/datasets/import-practice", status_code=201)
    def import_practice(body: PracticeImport, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        path = _allowed_sample(body.sample_path)
        view = _coord(request).import_practice(path, body.scoring_version)
        _record(request, "dataset.import_practice", "dataset", str(view.get("dataset_id")), "ok", {"actor": actor})
        return view

    @app.get("/api/v1/datasets/registered")
    def registered(request: Request) -> dict[str, object]:
        require_admin(request)
        return _reads(request).datasets()

    @app.get("/api/v1/samples")
    def samples(request: Request) -> dict[str, object]:
        require_admin(request)
        return {"items": [{"sample_id": item} for item in catalog_sample_ids()]}

    @app.post("/api/v1/datasets/import-sample", status_code=201)
    def import_sample(body: SampleImport, request: Request) -> dict[str, object]:
        actor = require_admin(request)
        path = practice_sample_path(body.sample_id)
        view = _coord(request).import_practice(path, body.scoring_version)
        _record(request, "dataset.import_sample", "dataset", str(view.get("dataset_id")), "ok", {"actor": actor})
        return view


def _coord(request: Request) -> Coordinator:
    return request.app.state.coordinator


def _reads(request: Request) -> DashboardReads:
    reads = getattr(request.app.state, "reads", None)
    if reads is None:
        reads = DashboardReads(_coord(request))
        request.app.state.reads = reads
    return reads


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header.split(" ", 1)[1].strip()
    return token or None


def _session_payload(request: Request) -> dict | None:
    sessions: SessionAuth = request.app.state.sessions
    return sessions.parse(request.cookies.get("minos_session"))


def require_admin(request: Request) -> str:
    """Return the actor name. Bearer tokens skip CSRF; cookie writes do not."""
    if request.headers.get("authorization"):
        token = _bearer(request)
        _coord(request).require_admin_token(token, loopback=False)
        return "admin-token"
    payload = _session_payload(request)
    if payload is not None:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            presented = request.headers.get("x-csrf-token", "")
            if not presented or not hmac_equal(presented, str(payload["csrf"])):
                raise ServiceError(403, "csrf", "CSRF token was rejected")
        return str(payload["u"])
    _coord(request).require_admin_token(None, loopback=_loopback(request))
    return "loopback"


def hmac_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1"}


def _idem(request: Request) -> str | None:
    value = request.headers.get("idempotency-key")
    if value is None:
        return None
    text = value.strip()
    return text[:128] or None


def _cookies(response: Response, session_token: str, csrf: str) -> None:
    secure = os.environ.get("DASHBOARD_COOKIE_SECURE") == "1"
    response.set_cookie(
        "minos_session",
        session_token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        "minos_csrf",
        csrf,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _record(request: Request, action: str, target_type: str, target_id: str, result: str, detail: dict) -> None:
    actor = detail.pop("actor", None) or "anonymous"
    with _coord(request)._db() as db:
        _audit(
            db,
            actor=str(actor),
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            detail=detail,
        )


def catalog_sample_ids() -> list[str]:
    path = repo_root() / "tuning" / "minos_tuning" / "configs" / "sample_id.md"
    if not path.is_file():
        raise ServiceError(404, "not_found", "sample catalog was not found")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def practice_sample_path(sample_id: str) -> Path:
    if sample_id not in set(catalog_sample_ids()):
        raise ServiceError(404, "not_found", "sample id is not in the catalog of 75")
    root = (repo_root() / "datasets" / "practice").resolve()
    for path in sorted(root.glob("round_*/sample.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("sample_id") == sample_id:
            return path.resolve()
    raise ServiceError(404, "not_found", "that sample is in the catalog but has no local sample.json")


def _allowed_sample(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    roots = []
    extra = os.environ.get("MINOS_DATASET_ROOTS", "")
    for item in extra.split(os.pathsep):
        if item.strip():
            roots.append(Path(item).expanduser().resolve())
    roots.append((repo_root() / "datasets").resolve())
    if not any(path == root or root in path.parents for root in roots):
        raise ServiceError(400, "path", "sample path is outside the allowed import roots")
    if not path.is_file():
        raise ServiceError(404, "not_found", "sample file was not found")
    return path
