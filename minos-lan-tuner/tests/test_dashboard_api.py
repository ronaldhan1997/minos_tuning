"""Dashboard auth, worker scheduling, graceful stop, and read models."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.coordinator.app import create_app
from apps.coordinator.auth import hash_password
from apps.coordinator.clock import iso, utc_now
from apps.coordinator.present import best_so_far, eta_seconds, presence_status
from apps.worker.runner import identity, register
from apps.worker.settings import WorkerSettings
from database.models import Job, Worker
from database.session import session
from datetime import timedelta
from integrations.minos.pin import head_commit, scorer_name_for
from tests.conftest import synthetic_manifest


def _app(url: str, monkeypatch, password: str = "correct horse battery") -> TestClient:
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD_HASH", hash_password(password))
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DASHBOARD_LOGIN_LIMIT", "3")
    app = create_app(url, "test-admin", lease_grace_seconds=30)
    return TestClient(app)


def _import(app, tmp_path: Path, dataset_id: str) -> None:
    root = tmp_path / dataset_id
    root.mkdir()
    app.state.coordinator.import_document(synthetic_manifest(root, dataset_id))


def _settings(tmp_path: Path, worker_id: str, token: str, platform: str = "linux") -> WorkerSettings:
    return WorkerSettings(
        coordinator_url="http://coordinator",
        worker_token=token,
        worker_id=worker_id,
        cache_dir=tmp_path / worker_id / "cache",
        work_dir=tmp_path / worker_id / "work",
        max_concurrent_jobs=2,
        execution_mode="mock",
        scoring_version="v1",
        log_level="WARNING",
        cache_max_bytes=10_000_000,
        max_memory_gb=64,
        max_timeout_seconds=3600,
    )


def _register(app, tmp_path: Path, worker_id: str, platform: str = "linux") -> tuple[TestClient, str]:
    token = app.state.coordinator.issue_token(worker_id)
    settings = _settings(tmp_path, worker_id, token, platform)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    register(client, settings)
    if platform != "linux":
        with session() as db:
            row = db.get(Worker, worker_id)
            assert row is not None
            row.platform = platform
            row.host_os = platform
            db.commit()
    return client, token


def test_login_csrf_and_bearer(database_url: str, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    denied = api.get("/api/v1/overview")
    assert denied.status_code == 401
    bad = api.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    logged = api.post("/api/v1/auth/login", json={"username": "admin", "password": "correct horse battery"})
    assert logged.status_code == 200
    csrf = logged.json()["csrf"]
    blocked = api.post("/api/v1/experiments/missing/pause")
    assert blocked.status_code == 403
    assert blocked.json()["error"] == "csrf"
    missing = api.post("/api/v1/experiments/missing/pause", headers={"X-CSRF-Token": csrf})
    assert missing.status_code == 404
    bearer = api.get("/api/v1/overview", headers={"Authorization": "Bearer test-admin"})
    assert bearer.status_code == 200
    assert bearer.json()["best_validated"] is None
    assert bearer.json()["jobs"]["failed"] == 0
    session = api.get("/api/v1/auth/session")
    assert session.json()["authenticated"] is True
    api.close()


def test_login_rate_limit(database_url: str, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    for _ in range(3):
        assert api.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    limited = api.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert limited.status_code == 429
    api.close()


def test_worker_pause_blocks_claim_and_windows_is_visible(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "sched",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
        },
    )
    assert created.status_code == 201
    ubuntu, _ = _register(api.app, tmp_path, "ubuntu-a")
    windows, _ = _register(api.app, tmp_path, "windows-b", platform="win32")
    paused = api.post("/api/v1/workers/ubuntu-a/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()["scheduling_mode"] == "paused"
    claim = {
        "cached_dataset_ids": [],
        "execution_mode": "mock",
        "free_disk_mb": 100000,
        "running_jobs": 0,
    }
    assert ubuntu.post("/api/v1/jobs/claim", json=claim).status_code == 204
    taken = windows.post("/api/v1/jobs/claim", json=claim)
    assert taken.status_code == 200
    listed = api.get("/api/v1/workers?q=windows", headers=headers)
    assert listed.status_code == 200
    match = [item for item in listed.json()["items"] if item["worker_id"] == "windows-b"]
    assert match[0]["presence"] == "online-busy"
    assert "Windows" in match[0]["runtime_note"]
    ubuntu_row = api.get("/api/v1/workers/ubuntu-a", headers=headers).json()
    assert ubuntu_row["presence"] == "paused"
    assert ubuntu_row["active_job_ids"] == []
    limited = api.patch(
        "/api/v1/workers/windows-b/policy",
        headers=headers,
        json={"max_concurrent_jobs": 1},
    )
    assert limited.status_code == 200
    assert limited.json()["effective_max_parallel"] == 1
    too_high = api.patch(
        "/api/v1/workers/windows-b/policy",
        headers=headers,
        json={"max_concurrent_jobs": 64},
    )
    assert too_high.status_code == 400
    ubuntu.close()
    windows.close()
    api.close()


def test_graceful_stop_keeps_the_leased_job_and_exports_training_winner(
    database_url: str, tmp_path: Path, monkeypatch
) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "stop-me",
            "trials": 2,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
            "max_parallel_global": 1,
        },
    )
    assert created.status_code == 201
    worker, _ = _register(api.app, tmp_path, "worker-a")
    claim = {
        "cached_dataset_ids": [],
        "execution_mode": "mock",
        "free_disk_mb": 100000,
        "running_jobs": 0,
    }
    first = worker.post("/api/v1/jobs/claim", json=claim)
    assert first.status_code == 200
    body = first.json()
    stopped = api.post("/api/v1/experiments/stop-me/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOPPING"
    assert worker.post("/api/v1/jobs/claim", json=claim).status_code == 204
    from integrations.minos.engine import mock_objective

    done = worker.post(
        f"/api/v1/jobs/{body['job_id']}/complete",
        json={
            "lease_token": body["lease_token"],
            "config_sha256": body["config_sha256"],
            "score_source": "mock",
            "official_score": None,
            "mock_objective": mock_objective(body["config_sha256"], body["dataset_id"]),
            "elapsed_seconds": 1,
        },
    )
    assert done.status_code == 200, done.text
    view = api.get("/api/v1/experiments/stop-me", headers=headers).json()
    assert view["status"] == "COMPLETED"
    exported = api.post("/api/v1/experiments/stop-me/export", headers=headers)
    assert exported.status_code == 200
    assert exported.json()["research_only"] is True
    download = api.get(exported.json()["downloads"]["conf"], headers=headers)
    assert download.status_code == 200
    assert "min_pruning=" in download.text
    assert "local_path" not in download.text
    provenance = api.get(exported.json()["downloads"]["provenance"], headers=headers)
    assert provenance.json()["research_only"] is True
    worker.close()
    api.close()


def test_stop_with_holdout_blocks_export(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    _import(api.app, tmp_path, "hold-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "held",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "held_out_dataset_ids": ["hold-a"],
            "execution_mode": "mock",
        },
    )
    assert created.status_code == 201
    stopped = api.post("/api/v1/experiments/held/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOPPED"
    denied = api.post("/api/v1/experiments/held/export", headers=headers)
    assert denied.status_code == 409
    assert denied.json()["error"] == "not_validated"
    api.close()


def test_stale_worker_retry_and_log(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    _import(api.app, tmp_path, "train-b")
    headers = {"Authorization": "Bearer test-admin"}
    api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "retry",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a", "train-b"],
            "execution_mode": "mock",
            "max_attempts": 2,
        },
    )
    worker, _ = _register(api.app, tmp_path, "worker-a")
    claim = {
        "cached_dataset_ids": [],
        "execution_mode": "mock",
        "free_disk_mb": 100000,
        "running_jobs": 0,
    }
    first = worker.post("/api/v1/jobs/claim", json=claim).json()
    failed = worker.post(
        f"/api/v1/jobs/{first['job_id']}/fail",
        json={
            "lease_token": first["lease_token"],
            "error_class": "gatk_failed",
            "message": "container exited",
            "retryable": False,
            "log_excerpt": "GATK error near /secret/not-shown.vcf",
        },
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "FAILED"
    logs = api.get(f"/api/v1/jobs/{first['job_id']}/logs", headers=headers)
    assert logs.status_code == 200
    assert "GATK error" in logs.json()["excerpt"]
    detail = api.get(f"/api/v1/jobs/{first['job_id']}", headers=headers).json()
    assert detail["official_score"] is None
    assert detail["events"]
    retried = api.post(
        f"/api/v1/jobs/{first['job_id']}/retry",
        headers={**headers, "Idempotency-Key": "retry-1"},
    )
    assert retried.status_code == 200
    assert retried.json()["state"] == "PENDING"
    again = api.post(
        f"/api/v1/jobs/{first['job_id']}/retry",
        headers={**headers, "Idempotency-Key": "retry-1"},
    )
    assert again.json()["attempt"] == retried.json()["attempt"]
    with session() as db:
        row = db.get(Worker, "worker-a")
        assert row is not None
        row.last_heartbeat = iso(utc_now() - timedelta(minutes=10))
        row.minos_commit = "0" * 40
        db.commit()
    viewed = api.get("/api/v1/workers/worker-a", headers=headers).json()
    assert viewed["presence"] == "offline"
    with session() as db:
        row = db.get(Worker, "worker-a")
        assert row is not None
        row.last_heartbeat = iso()
        db.commit()
    drifted = api.get("/api/v1/workers/worker-a", headers=headers).json()
    assert drifted["presence"] == "incompatible"
    audit = api.get("/api/v1/audit", headers=headers)
    assert audit.status_code == 200
    assert any(item["action"] == "job.retry" for item in audit.json()["items"])
    worker.close()
    api.close()


def test_score_projection_does_not_treat_missing_as_zero() -> None:
    series = best_so_far(
        [
            {"trial_id": "a", "optuna_number": 0, "objective": None, "told": True, "updated_at": "2020-01-01T00:00:00+00:00"},
            {"trial_id": "b", "optuna_number": 1, "objective": 0, "told": True, "updated_at": "2020-01-02T00:00:00+00:00", "score_source": "official", "is_baseline": False},
            {"trial_id": "c", "optuna_number": 2, "objective": 3, "told": True, "updated_at": "2020-01-03T00:00:00+00:00", "score_source": "official", "is_baseline": False},
        ]
    )
    assert [row["objective"] for row in series] == [0, 3]
    assert series[-1]["best_so_far"] == 3
    assert eta_seconds([1, 2], 4) is None
    assert eta_seconds([10, 10, 10], 2)["seconds"] == 20
    assert (
        presence_status(
            last_heartbeat=None,
            execution_mode="official",
            docker_ready=False,
            minos_commit="abc",
            pinned_commit="abc",
            scheduling_mode="enabled",
            active_jobs=0,
        )
        == "unknown"
    )


def test_sample_catalog_lists_the_practice_ids(database_url: str, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    listed = api.get("/api/v1/samples", headers={"Authorization": "Bearer test-admin"})
    assert listed.status_code == 200
    ids = [item["sample_id"] for item in listed.json()["items"]]
    assert len(ids) == 75
    assert "065f8426-92fc-411d-ac2d-9a661e7c7cba" in ids
    api.close()


def test_dashboard_can_issue_replace_and_revoke_a_computer_token(
    database_url: str, tmp_path: Path, monkeypatch
) -> None:
    api = _app(database_url, monkeypatch)
    logged = api.post("/api/v1/auth/login", json={"username": "admin", "password": "correct horse battery"})
    headers = {"X-CSRF-Token": logged.json()["csrf"]}
    assert api.get("/api/v1/workers/tokens").json()["items"] == []
    assert api.post("/api/v1/workers/tokens", json={"worker_id": "laptop-a"}).status_code == 403
    assert api.post("/api/v1/workers/tokens", headers=headers, json={"worker_id": "bad name"}).status_code == 422
    created = api.post("/api/v1/workers/tokens", headers=headers, json={"worker_id": "laptop-a"})
    assert created.status_code == 201
    first = created.json()["token"]
    listed = api.get("/api/v1/workers/tokens").json()["items"]
    assert [item["worker_id"] for item in listed] == ["laptop-a"]
    assert listed[0]["revoked"] is False
    assert listed[0]["registered"] is False
    assert "token" not in listed[0]
    assert "token_hash" not in listed[0]
    settings = _settings(tmp_path, "laptop-a", first)
    worker = TestClient(api.app, headers={"Authorization": f"Bearer {first}"})
    assert worker.post("/api/v1/workers/register", json=identity(settings).model_dump()).status_code == 200
    assert api.get("/api/v1/workers/tokens").json()["items"][0]["registered"] is True
    replaced = api.post("/api/v1/workers/tokens", headers=headers, json={"worker_id": "laptop-a"})
    second = replaced.json()["token"]
    assert second != first
    assert worker.post("/api/v1/workers/register", json=identity(settings).model_dump()).status_code == 401
    fresh = _settings(tmp_path, "laptop-a", second)
    renewed = {"Authorization": f"Bearer {second}"}
    assert worker.post("/api/v1/workers/register", headers=renewed, json=identity(fresh).model_dump()).status_code == 200
    revoked = api.post("/api/v1/workers/laptop-a/revoke-token", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert api.get("/api/v1/workers/tokens").json()["items"][0]["revoked"] is True
    assert worker.post("/api/v1/workers/register", headers=renewed, json=identity(fresh).model_dump()).status_code == 401
    missing = api.post("/api/v1/workers/missing-pc/revoke-token", headers=headers)
    assert missing.status_code == 404
    bearer = api.post(
        "/api/v1/workers/tokens",
        headers={"Authorization": "Bearer test-admin"},
        json={"worker_id": "laptop-b"},
    )
    assert bearer.status_code == 201
    worker.close()
    api.close()


def test_metrics_report_job_pace(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "pace",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
            "start": False,
        },
    )
    assert created.status_code == 201
    metrics = api.get("/api/v1/experiments/pace/metrics", headers=headers)
    assert metrics.status_code == 200
    body = metrics.json()
    assert body["throughput_completed_jobs_last_hour"] == 0
    assert body["mean_elapsed_seconds"] is None
    assert body["eta"] is None
    assert body["best_so_far"] == []
    api.close()


def test_running_log_follows_the_active_job(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "live-log",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
            "start": True,
        },
    )
    assert created.status_code == 201
    worker, _token = _register(api.app, tmp_path, "logger")
    claimed = worker.post(
        "/api/v1/jobs/claim",
        json={"cached_dataset_ids": [], "execution_mode": "mock", "free_disk_mb": 100000, "running_jobs": 0},
    ).json()
    beat = worker.post(
        "/api/v1/workers/heartbeat",
        json={
            "active_job_ids": [claimed["job_id"]],
            "log_excerpt": "ProgressMeter chr22:31236092",
            "cached_dataset_ids": [],
        },
    )
    assert beat.status_code == 200
    shown = api.get("/api/v1/experiments/live-log/log", headers=headers)
    assert shown.status_code == 200
    assert shown.json()["items"][0]["excerpt"] == "ProgressMeter chr22:31236092"
    assert api.get("/api/v1/experiments/live-log/log").status_code == 401

    monkeypatch.setattr(
        "apps.coordinator.reads.local_container_log",
        lambda job_id: "HaplotypeCaller traversal" if job_id == claimed["job_id"] else None,
    )
    with session() as db:
        row = db.get(Job, claimed["job_id"])
        assert row is not None
        row.log_excerpt = None
        db.commit()
    fallback = api.get("/api/v1/experiments/live-log/log", headers=headers).json()
    assert fallback["items"][0]["excerpt"] == "HaplotypeCaller traversal"
    worker.close()
    api.close()


def test_check_in_without_a_job_clears_a_stale_running_job(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "stale-job",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
            "start": True,
        },
    )
    assert created.status_code == 201
    worker, _token = _register(api.app, tmp_path, "laptop")
    claimed = worker.post(
        "/api/v1/jobs/claim",
        json={"cached_dataset_ids": [], "execution_mode": "mock", "free_disk_mb": 100000, "running_jobs": 0},
    ).json()
    with session() as db:
        row = db.get(Job, claimed["job_id"])
        assert row is not None
        row.state = "RUNNING"
        row.updated_at = iso(utc_now() - timedelta(minutes=3))
        db.commit()
    beat = worker.post("/api/v1/workers/heartbeat", json={"active_job_ids": [], "cached_dataset_ids": []})
    assert beat.status_code == 200
    view = api.get("/api/v1/workers/laptop", headers=headers).json()
    assert view["presence"] == "online-idle"
    assert view["active_job_ids"] == []
    again = worker.post(
        "/api/v1/jobs/claim",
        json={"cached_dataset_ids": [], "execution_mode": "mock", "free_disk_mb": 100000, "running_jobs": 0},
    )
    assert again.status_code == 200
    assert again.json()["job_id"] == claimed["job_id"]
    released = api.get(f"/api/v1/jobs/{claimed['job_id']}", headers=headers).json()
    assert released["error_class"] is None
    fresh = worker.post(
        "/api/v1/workers/heartbeat",
        json={"active_job_ids": [claimed["job_id"]], "cached_dataset_ids": []},
    )
    assert fresh.status_code == 200
    kept = api.get(f"/api/v1/jobs/{claimed['job_id']}", headers=headers).json()
    assert kept["state"] == "RUNNING"
    worker.close()
    api.close()


def test_heartbeat_records_cpu_memory_and_disk(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    worker, _token = _register(api.app, tmp_path, "usage-a")
    headers = {"Authorization": "Bearer test-admin"}
    before = api.get("/api/v1/workers/usage-a", headers=headers).json()
    beat = worker.post(
        "/api/v1/workers/heartbeat",
        json={
            "active_job_ids": [],
            "cpu_percent": 37.2,
            "ram_used_mb": 4096,
            "free_disk_mb": 10000,
            "disk_total_mb": 40000,
            "cached_dataset_ids": [],
        },
    )
    assert beat.status_code == 200
    after = api.get("/api/v1/workers/usage-a", headers=headers).json()
    assert after["cpu_percent"] == 37.2
    assert after["ram_used_mb"] == 4096
    assert after["ram_mb"] == before["ram_mb"]
    assert after["free_disk_mb"] == 10000
    assert after["disk_total_mb"] == 40000
    worker.close()
    api.close()


def test_presence_check_in_survives_a_failed_report(tmp_path: Path, monkeypatch) -> None:
    import threading

    from apps.worker import runner

    calls: list[tuple[list[str], float | None]] = []
    stop = threading.Event()

    def beat(client, settings, active, log_excerpt=None, timeout=None):
        calls.append((list(active), timeout))
        if len(calls) == 1:
            raise RuntimeError("connection stalled")
        stop.set()

    class Dummy:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(runner, "_heartbeat", beat)
    monkeypatch.setattr(runner, "make_client", lambda settings: Dummy())
    monkeypatch.setattr(runner, "_job_container_log", lambda job_id: (_ for _ in ()).throw(AssertionError(job_id)))
    runner._set_current_job("job-1")
    try:
        runner._presence_loop(_settings(tmp_path, "laptop", "token"), stop, interval=0.01)
    finally:
        runner._set_current_job(None)
    assert [active for active, _timeout in calls] == [["job-1"], ["job-1"]]
    assert calls[-1][1] == 15.0


def test_host_usage_reads_this_computer(tmp_path: Path) -> None:
    import time

    from apps.worker.doctor import host_usage

    first = host_usage(tmp_path)
    assert first["cpu_percent"] is None
    assert int(first["disk_total_mb"] or 0) >= int(first["free_disk_mb"] or 0)
    assert int(first["ram_total_mb"] or 0) >= int(first["ram_used_mb"] or 0) > 0
    time.sleep(0.3)
    second = host_usage(tmp_path)
    assert second["cpu_percent"] is not None
    assert 0 <= float(second["cpu_percent"]) <= 100


def test_worker_downloads_the_running_study(database_url: str, tmp_path: Path, monkeypatch) -> None:
    from apps.worker.runner import adopt_server_config

    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    token = api.app.state.coordinator.issue_token("laptop-a")
    worker = {"Authorization": f"Bearer {token}"}
    idle = api.get("/api/v1/workers/config", headers=worker)
    assert idle.status_code == 200
    assert idle.json()["available"] is False
    assert api.get("/api/v1/workers/config").status_code == 401
    created = api.post(
        "/api/v1/experiments",
        headers={"Authorization": "Bearer test-admin"},
        json={
            "name": "study-a",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
            "start": True,
        },
    )
    assert created.status_code == 201
    downloaded = api.get("/api/v1/workers/config", headers=worker)
    assert downloaded.status_code == 200
    body = downloaded.json()
    assert body["available"] is True
    assert body["execution_mode"] == "mock"
    assert body["scoring_version"] == "v1"
    assert body["experiment_name"] == "study-a"
    assert body["gatk_image"]
    assert body["gatk_image_id"]
    settings = _settings(tmp_path, "laptop-a", token)
    adopted = adopt_server_config(settings, {"execution_mode": "official", "scoring_version": "v2"})
    assert adopted.execution_mode == "official"
    assert adopted.scoring_version == "v2"
    assert settings.execution_mode == "mock"
    api.close()


def test_missing_gatk_image_is_downloaded(monkeypatch) -> None:
    from apps.worker import runner

    present: set[str] = set()

    def inspect(image: str) -> str | None:
        return "sha256:present" if image in present else None

    def pull(image: str, announce: object) -> tuple[bool, str]:
        present.add(image)
        if callable(announce):
            announce(f"Downloading {image} — 40% (1.0 GB / 2.5 GB)")
        return True, ""

    monkeypatch.setattr(runner, "inspect_image_id", inspect)
    monkeypatch.setattr(runner, "docker_executable", lambda: "docker")
    monkeypatch.setattr(runner, "pull_image", pull)
    monkeypatch.setattr(runner, "_pull_not_before", 0.0)
    assert runner.ensure_study_images(["broadinstitute/gatk:4.5.0.0"]) is True
    assert "broadinstitute/gatk:4.5.0.0" in present
    assert runner.ensure_study_images(["broadinstitute/gatk:4.5.0.0"]) is True


def test_windows_client_finds_docker_desktop_and_hides_its_console(monkeypatch, tmp_path) -> None:
    from integrations.minos import pin

    install = tmp_path / "Docker" / "Docker" / "resources" / "bin"
    install.mkdir(parents=True)
    (install / "docker.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr(pin.sys, "platform", "win32")
    monkeypatch.setattr(pin.shutil, "which", lambda _name: None)
    assert pin.docker_executable() == str(install / "docker.exe")

    seen: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        seen["command"] = command
        seen["kwargs"] = kwargs

        class Result:
            returncode = 0
            stdout = "sha256:abc\n"
            stderr = ""

        return Result()

    monkeypatch.setattr(pin.subprocess, "run", fake_run)
    assert pin.inspect_image_id("broadinstitute/gatk:4.5.0.0") == "sha256:abc"
    assert seen["command"] == [str(install / "docker.exe"), "image", "inspect", "--format", "{{.Id}}", "broadinstitute/gatk:4.5.0.0"]
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["stdin"] is pin.subprocess.DEVNULL


def test_openapi_contains_dashboard_routes(database_url: str, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    schema = api.get("/openapi.json").json()
    paths = schema["paths"]
    for path in (
        "/api/v1/overview",
        "/api/v1/auth/login",
        "/api/v1/workers/{worker_id}/drain",
        "/api/v1/experiments/{key}/stop",
        "/api/v1/experiments/{key}/metrics",
        "/api/v1/jobs/{job_id}/logs",
        "/api/v1/exports/{export_id}/download",
    ):
        assert path in paths
    api.close()


def test_draft_does_not_ask_until_start(database_url: str, tmp_path: Path, monkeypatch) -> None:
    api = _app(database_url, monkeypatch)
    _import(api.app, tmp_path, "train-a")
    headers = {"Authorization": "Bearer test-admin"}
    created = api.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "drafty",
            "trials": 1,
            "scoring_version": "v1",
            "training_dataset_ids": ["train-a"],
            "execution_mode": "mock",
            "start": False,
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "DRAFT"
    assert created.json()["trials_total"] == 0
    worker, _ = _register(api.app, tmp_path, "worker-a")
    claim = {"cached_dataset_ids": [], "execution_mode": "mock", "free_disk_mb": 100000, "running_jobs": 0}
    assert worker.post("/api/v1/jobs/claim", json=claim).status_code == 204
    started = api.post("/api/v1/experiments/drafty/start", headers=headers)
    assert started.status_code == 200
    assert started.json()["status"] == "RUNNING"
    assert worker.post("/api/v1/jobs/claim", json=claim).status_code == 200
    with session() as db:
        assert db.query(Job).count() >= 1
    worker.close()
    api.close()
