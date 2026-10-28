"""Lease, recovery, cache, and Optuna tell-once behavior. Execution is mock."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import optuna
import pytest
from fastapi.testclient import TestClient
from optuna.trial import TrialState

from apps.coordinator.app import create_app
from apps.coordinator.clock import iso, utc_now
from apps.coordinator.objective import objective_value
from apps.worker.runner import register, run_once
from apps.worker.settings import WorkerSettings
from database.models import Job
from database.session import session
from integrations.minos.engine import mock_objective
from integrations.minos.pin import gatk_image, happy_image, head_commit, inspect_image_id, scorer_name_for
from shared.schemas import ExperimentCreate
from tests.conftest import synthetic_manifest


def _app(url: str):
    return create_app(url, "test-admin", lease_grace_seconds=30)


def _import(coord, tmp_path: Path, dataset_id: str, version: str = "v1") -> None:
    root = tmp_path / dataset_id
    root.mkdir()
    coord.import_document(synthetic_manifest(root, dataset_id, scoring_version=version))


def _settings(tmp_path: Path, worker_id: str, token: str, version: str = "v1") -> WorkerSettings:
    return WorkerSettings(
        coordinator_url="http://coordinator",
        worker_token=token,
        worker_id=worker_id,
        cache_dir=tmp_path / worker_id / "cache",
        work_dir=tmp_path / worker_id / "work",
        max_concurrent_jobs=2,
        execution_mode="mock",
        scoring_version=version,
        log_level="WARNING",
        cache_max_bytes=10_000_000,
        max_memory_gb=64,
        max_timeout_seconds=3600,
    )


def _worker(app, settings: WorkerSettings) -> TestClient:
    client = TestClient(app, headers={"Authorization": f"Bearer {settings.worker_token}"})
    register(client, settings)
    return client


def _drain(app, workers: list[tuple[TestClient, WorkerSettings]], limit: int = 80) -> None:
    for _ in range(limit):
        progressed = False
        for client, settings in workers:
            if run_once(client, settings):
                progressed = True
        if not progressed:
            return


def test_distributed_mock_run_tells_once_and_exports(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    _import(coord, tmp_path, "train-b")
    _import(coord, tmp_path, "hold-c")
    created = coord.create_experiment(
        ExperimentCreate(
            name="gatk-v2",
            trials=10,
            scoring_version="v1",
            training_dataset_ids=["train-a", "train-b"],
            held_out_dataset_ids=["hold-c"],
            execution_mode="mock",
            variability_penalty=0.5,
            top_n_validate=2,
            max_parallel_global=40,
            seed=7,
        )
    )
    assert created["execution_mode"] == "mock"
    token_a = coord.issue_token("worker-a")
    token_b = coord.issue_token("worker-b")
    settings_a = _settings(tmp_path, "worker-a", token_a)
    settings_b = _settings(tmp_path, "worker-b", token_b)
    client_a = _worker(app, settings_a)
    client_b = _worker(app, settings_b)
    _drain(app, [(client_a, settings_a), (client_b, settings_b)])
    view = coord.get_experiment("gatk-v2")
    assert view["status"] == "COMPLETED"
    assert view["trials_told"] == 10
    assert view["best_score_source"] == "mock"
    jobs = coord.trial_rows("gatk-v2")["trials"]
    flat = [job for trial in jobs for job in trial["jobs"]]
    assert len([job for job in flat if job["role"] == "training"]) == 20
    assert {job["worker_id"] for job in flat if job["state"] == "COMPLETED"} >= {"worker-a", "worker-b"}
    study = optuna.load_study(study_name=view["id"], storage=coord.optuna_storage_url())
    finished = [trial for trial in study.trials if trial.state == TrialState.COMPLETE]
    assert len(finished) == 10
    # Held-out scores are a different dataset and must not be the told value.
    training = [trial for trial in jobs if trial["optuna_number"] == 0][0]
    training_scores = [
        job["mock_objective"]
        for job in training["jobs"]
        if job["role"] == "training"
    ]
    expected = objective_value(training_scores, 0.5)
    told = next(trial.value for trial in study.trials if trial.number == 0)
    assert told == pytest.approx(expected)
    text = coord.export_conf("gatk-v2")
    assert "min_pruning=" in text
    assert "mock" in text
    assert view["selection"]["rule"]
    client_a.close()
    client_b.close()


def test_parallel_cap_stale_lease_and_restart(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    _import(coord, tmp_path, "train-b")
    coord.create_experiment(
        ExperimentCreate(
            name="cap",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a", "train-b"],
            execution_mode="mock",
            max_parallel_global=1,
            max_attempts=2,
            job_timeout_seconds=30,
        )
    )
    token = coord.issue_token("worker-a")
    settings = _settings(tmp_path, "worker-a", token)
    client = _worker(app, settings)
    claim_body = {"cached_dataset_ids": [], "execution_mode": "mock", "free_disk_mb": 100000, "running_jobs": 0}
    first = client.post("/api/v1/jobs/claim", json=claim_body)
    second = client.post("/api/v1/jobs/claim", json=claim_body)
    assert first.status_code == 200
    assert second.status_code == 204
    db = session()
    job = db.query(Job).filter(Job.state.in_(("LEASED", "RUNNING"))).one()
    stale = job.lease_token
    job_id = job.id
    job.lease_expires_at = iso(utc_now() - timedelta(seconds=5))
    db.commit()
    db.close()
    token_b = coord.issue_token("worker-b")
    settings_b = _settings(tmp_path, "worker-b", token_b)
    client_b = _worker(app, settings_b)
    assert run_once(client_b, settings_b) is True
    stale_response = client.post(
        f"/api/v1/jobs/{job_id}/complete",
        json={
            "lease_token": stale,
            "config_sha256": "0" * 64,
            "score_source": "mock",
            "mock_objective": 1,
            "official_score": None,
            "elapsed_seconds": 1,
            "provenance": {},
        },
    )
    assert stale_response.status_code == 409
    _drain(app, [(client, settings), (client_b, settings_b)])
    assert coord.get_experiment("cap")["status"] == "COMPLETED"
    view = coord.get_experiment("cap")
    # Restart must not tell the finished trial again.
    values = [
        trial.value
        for trial in optuna.load_study(study_name=view["id"], storage=coord.optuna_storage_url()).trials
    ]
    restarted = _app(database_url)
    again = [
        trial.value
        for trial in optuna.load_study(
            study_name=view["id"], storage=restarted.state.coordinator.optuna_storage_url()
        ).trials
    ]
    assert again == values
    assert restarted.state.coordinator.get_experiment("cap")["status"] == "COMPLETED"
    client.close()
    client_b.close()


def test_crash_before_tell_is_reconciled(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    created = coord.create_experiment(
        ExperimentCreate(
            name="recover",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a"],
            execution_mode="mock",
            max_parallel_global=4,
        )
    )
    db = session()
    job = db.query(Job).one()
    from database.models import Trial

    trial = db.query(Trial).one()
    job.state = "COMPLETED"
    job.mock_objective = mock_objective(job.config_sha256, job.dataset_id)
    job.score_source = "mock"
    job.official_score = None
    trial.told = False
    trial.state = "RUNNING"
    db.commit()
    db.close()
    restarted = _app(database_url)
    view = restarted.state.coordinator.get_experiment("recover")
    assert view["status"] == "COMPLETED"
    assert view["trials_told"] == 1
    study = optuna.load_study(study_name=created["id"], storage=restarted.state.coordinator.optuna_storage_url())
    assert study.trials[0].state == TrialState.COMPLETE
    assert study.trials[0].value == pytest.approx(job.mock_objective)


def test_orphan_ask_is_adopted(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    created = coord.create_experiment(
        ExperimentCreate(
            name="orphan",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a"],
            execution_mode="mock",
        )
    )
    from integrations.minos.search import load_search_space, suggest
    from integrations.minos.search import default_search_path

    study = optuna.load_study(study_name=created["id"], storage=coord.optuna_storage_url())
    orphan = study.ask()
    suggest(orphan, load_search_space(default_search_path(), "stage1"))
    restarted = _app(database_url)
    rows = restarted.state.coordinator.trial_rows("orphan")["trials"]
    numbers = {row["optuna_number"] for row in rows}
    assert orphan.number in numbers
    adopted = next(row for row in rows if row["optuna_number"] == orphan.number)
    assert adopted["jobs"]
    assert adopted["told"] is False


def test_cache_hit_skips_a_second_execution(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    coord.create_experiment(
        ExperimentCreate(
            name="first",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a"],
            execution_mode="mock",
        )
    )
    token = coord.issue_token("worker-a")
    settings = _settings(tmp_path, "worker-a", token)
    client = _worker(app, settings)
    assert run_once(client, settings) is True
    assert coord.get_experiment("first")["status"] == "COMPLETED"
    second = coord.create_experiment(
        ExperimentCreate(
            name="second",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a"],
            execution_mode="mock",
        )
    )
    assert second["status"] == "COMPLETED"
    assert second["job_counts"].get("PENDING", 0) == 0
    rows = coord.trial_rows("second")["trials"][0]["jobs"]
    assert rows[0]["worker_id"] is None
    client.close()


def test_failed_job_is_not_a_zero_score(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    created = coord.create_experiment(
        ExperimentCreate(
            name="bad",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a"],
            execution_mode="mock",
            max_attempts=3,
        )
    )
    token = coord.issue_token("worker-a")
    settings = _settings(tmp_path, "worker-a", token)
    client = _worker(app, settings)
    claimed = client.post(
        "/api/v1/jobs/claim",
        json={
            "cached_dataset_ids": [],
            "execution_mode": "mock",
            "free_disk_mb": 100000,
            "running_jobs": 0,
        },
    )
    body = claimed.json()
    failed = client.post(
        f"/api/v1/jobs/{body['job_id']}/fail",
        json={
            "lease_token": body["lease_token"],
            "error_class": "invalid_config",
            "message": "validator rejected the config",
            "retryable": True,
            "details": {"discarded_official_score": 0},
        },
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "FAILED"
    view = coord.get_experiment("bad")
    assert view["trials_told"] == 1
    study = optuna.load_study(study_name=created["id"], storage=coord.optuna_storage_url())
    assert study.trials[0].state == TrialState.FAIL
    assert study.trials[0].value is None
    client.close()


def test_api_auth_range_and_pause(database_url: str, tmp_path: Path) -> None:
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    with TestClient(app) as api:
        denied = api.post(
            "/api/v1/experiments",
            json={
                "name": "nope",
                "trials": 1,
                "scoring_version": "v1",
                "training_dataset_ids": ["train-a"],
                "execution_mode": "mock",
            },
        )
        assert denied.status_code == 401
        headers = {"Authorization": "Bearer test-admin"}
        created = api.post(
            "/api/v1/experiments",
            headers=headers,
            json={
                "name": "paused",
                "trials": 1,
                "scoring_version": "v1",
                "training_dataset_ids": ["train-a"],
                "execution_mode": "mock",
            },
        )
        assert created.status_code == 201
        assert api.post("/api/v1/experiments/paused/pause", headers=headers).status_code == 200
        manifest = api.get("/api/v1/datasets/train-a/manifest", headers=headers)
        assert manifest.status_code == 200
        assert "local_path" not in manifest.text
        item = manifest.json()["files"][0]
        ranged = api.get(
            f"/api/v1/datasets/train-a/blobs/{item['sha256']}",
            headers={**headers, "Range": "bytes=0-1"},
        )
        assert ranged.status_code == 206
        assert ranged.headers["content-range"].startswith("bytes 0-1/")
        assert len(ranged.content) == 2
        page = api.get("/legacy", headers=headers)
        assert page.status_code == 200
        assert "paused" in page.text
        assert api.get("/").status_code == 200
        assert api.get("/healthz").json()["status"] == "ok"
        assert api.get("/readyz").json()["status"] == "ready"


def test_cli_import_and_status(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.coordinator.cli import main

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("COORDINATOR_ADMIN_TOKEN", "test-admin")
    root = tmp_path / "sample"
    root.mkdir()
    manifest = tmp_path / "dataset_manifest.json"
    import json

    json.dump = json.dump
    manifest.write_text(
        __import__("json").dumps(synthetic_manifest(root, "cli-sample")),
        encoding="utf-8",
    )
    assert main(["dataset", "import", "--manifest", str(manifest)]) == 0
    assert (
        main(
            [
                "experiment",
                "create",
                "--name",
                "cli-exp",
                "--trials",
                "1",
                "--scoring-version",
                "v1",
                "--train",
                "cli-sample",
                "--execution-mode",
                "mock",
                "--search-space",
                str(Path(__file__).resolve().parents[1] / "config" / "search_space.yaml"),
            ]
        )
        == 0
    )
    assert main(["experiment", "status", "cli-exp"]) == 0


def test_official_claim_requires_the_same_image_digest(database_url: str, tmp_path: Path) -> None:
    image_id = inspect_image_id(gatk_image())
    if not image_id:
        pytest.skip("GATK image is not present")
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a")
    coord.create_experiment(
        ExperimentCreate(
            name="official-pin",
            trials=1,
            scoring_version="v1",
            training_dataset_ids=["train-a"],
            execution_mode="official",
            max_parallel_global=1,
        )
    )
    good = coord.issue_token("good")
    bad = coord.issue_token("bad")
    with TestClient(app) as api:
        for worker_id, token, digest in (("good", good, image_id), ("bad", bad, "sha256:not-the-image")):
            registered = api.post(
                "/api/v1/workers/register",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "worker_id": worker_id,
                    "hostname": "test",
                    "host_os": "linux",
                    "platform": "linux",
                    "cpu_count": 8,
                    "ram_mb": 16000,
                    "free_disk_mb": 100000,
                    "max_parallel_jobs": 1,
                    "docker_ready": True,
                    "gatk_image": gatk_image(),
                    "gatk_image_id": digest,
                    "happy_image": happy_image(),
                    "scorer_version": "v1",
                    "scorer_name": scorer_name_for("v1"),
                    "minos_commit": head_commit(),
                    "execution_mode": "official",
                },
            )
            assert registered.status_code == 200, registered.text
        wrong = api.post(
            "/api/v1/jobs/claim",
            headers={"Authorization": f"Bearer {bad}"},
            json={"execution_mode": "official", "free_disk_mb": 100000, "running_jobs": 0},
        )
        assert wrong.status_code == 204
        right = api.post(
            "/api/v1/jobs/claim",
            headers={"Authorization": f"Bearer {good}"},
            json={"execution_mode": "official", "free_disk_mb": 100000, "running_jobs": 0},
        )
        assert right.status_code == 200
        assert right.json()["gatk_image_id"] == image_id
        assert right.json()["minos_commit"] == head_commit()
        assert right.json()["threads"] == 7
        assert right.json()["memory_gb"] == 13
    coord.cancel("official-pin")


def test_uploaded_happy_vcf_is_scored_on_the_coordinator(database_url: str, tmp_path: Path, monkeypatch) -> None:
    import base64

    monkeypatch.setattr("apps.coordinator.service.inspect_image_id", lambda _image: "sha256:test")
    app = _app(database_url)
    coord = app.state.coordinator
    _import(coord, tmp_path, "train-a", "v2")
    coord.create_experiment(
        ExperimentCreate(
            name="windows-score",
            trials=1,
            scoring_version="v2",
            training_dataset_ids=["train-a"],
            execution_mode="official",
            max_parallel_global=1,
        )
    )
    token = coord.issue_token("desktop")
    vcf = (
        "##fileformat=VCFv4.2\n"
        "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
        "##FORMAT=<ID=BD,Number=1,Type=String,Description=\"Decision\">\n"
        "##FORMAT=<ID=BVT,Number=1,Type=String,Description=\"Type\">\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
        "chr20\t100\t.\tA\tG\t.\tPASS\t.\tGT:BD:BVT\t0/1:TP:SNP\t0/1:TP:SNP\n"
    )
    metrics = {
        "f1_snp": 0.95,
        "f1_indel": 0.95,
        "recall_snp": 0.95,
        "recall_indel": 0.95,
        "truth_total_snp": 100,
        "truth_total_indel": 50,
        "query_total_snp": 100,
        "query_total_indel": 50,
        "fp_snp": 2,
        "fp_indel": 1,
        "frac_na_snp": 0.0,
        "frac_na_indel": 0.0,
        "titv_truth_snp": 2.1,
        "titv_query_snp": 2.1,
        "hethom_truth_snp": 1.5,
        "hethom_query_snp": 1.5,
        "hethom_truth_indel": 1.5,
        "hethom_query_indel": 1.5,
        "fp_per_target": 0.2,
        "happy_vcf_path": r"C:\Users\Admin\AppData\Local\MinosClient\work\job\happy_output.vcf.gz",
    }
    with TestClient(app) as api:
        registered = api.post(
            "/api/v1/workers/register",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "worker_id": "desktop",
                "hostname": "DESKTOP",
                "host_os": "Windows",
                "platform": "win32",
                "cpu_count": 8,
                "ram_mb": 32000,
                "free_disk_mb": 100000,
                "max_parallel_jobs": 1,
                "docker_ready": True,
                "gatk_image": gatk_image(),
                "gatk_image_id": "sha256:test",
                "happy_image": happy_image(),
                "scorer_version": "v2",
                "scorer_name": scorer_name_for("v2"),
                "minos_commit": head_commit(),
                "execution_mode": "official",
            },
        )
        assert registered.status_code == 200, registered.text
        claimed = api.post(
            "/api/v1/jobs/claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"execution_mode": "official", "free_disk_mb": 100000, "running_jobs": 0},
        )
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        failed = api.post(
            f"/api/v1/jobs/{body['job_id']}/fail",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "lease_token": body["lease_token"],
                "error_class": "scoring_unavailable",
                "message": "v2 could not be computed because this app cannot import pysam",
                "retryable": False,
                "details": metrics,
                "happy_vcf_b64": base64.b64encode(vcf.encode()).decode("ascii"),
            },
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["state"] == "COMPLETED"
    view = coord.get_experiment("windows-score")
    assert view["trials_told"] == 1
    assert view["best_objective"] is not None
    assert 0 < view["best_objective"] <= 100


def test_usable_resources_leave_the_computer_room() -> None:
    from shared.resources import usable_resources

    threads, memory_gb = usable_resources(cpu_count=12, ram_mb=64000, ram_used_mb=8000)
    assert threads == 11
    assert memory_gb == (64000 - 8000 - 2048) // 1024
    busy_threads, busy_memory = usable_resources(cpu_count=4, ram_mb=8000, ram_used_mb=7000)
    assert busy_threads == 3
    assert busy_memory == 1
    single, _memory = usable_resources(cpu_count=1, ram_mb=2048, ram_used_mb=None)
    assert single == 1


def test_doctor_does_not_claim_windows_was_tested(tmp_path: Path) -> None:
    from apps.worker.doctor import run_doctor

    report = run_doctor(None, probe_coordinator=False)
    windows = next(item for item in report["checks"] if item["name"] == "windows_hardware")
    assert windows["required"] is False
    assert windows["ok"] is False
    assert "not" in windows["detail"].lower() or "has not" in windows["detail"].lower()
