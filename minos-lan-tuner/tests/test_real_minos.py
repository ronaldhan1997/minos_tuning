"""Opt-in GATK + hap.py run. This does not execute unless MINOS_REAL=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.coordinator.app import create_app
from apps.worker.runner import register, run_once
from apps.worker.settings import WorkerSettings
from fastapi.testclient import TestClient
from integrations.minos.pin import head_commit
from shared.schemas import ExperimentCreate

pytestmark = pytest.mark.real_minos


@pytest.mark.skipif(os.environ.get("MINOS_REAL") != "1", reason="set MINOS_REAL=1 to run GATK and hap.py")
def test_one_practice_sample(tmp_path: Path) -> None:
    version = os.environ.get("MINOS_SCORING_VERSION")
    sample = os.environ.get("MINOS_PRACTICE_SAMPLE")
    if version not in {"v1", "v2"} or not sample:
        pytest.skip("set MINOS_SCORING_VERSION=v1|v2 and MINOS_PRACTICE_SAMPLE to a sample.json")
    url = f"sqlite:///{tmp_path}/real.sqlite3"
    app = create_app(url, "test-admin", lease_grace_seconds=120)
    coord = app.state.coordinator
    imported = coord.import_practice(Path(sample), version)
    created = coord.create_experiment(
        ExperimentCreate(
            name="real-one",
            trials=1,
            scoring_version=version,  # type: ignore[arg-type]
            training_dataset_ids=[imported["dataset_id"]],
            execution_mode="official",
            max_parallel_global=1,
            job_timeout_seconds=int(os.environ.get("MINOS_REAL_TIMEOUT", "1800")),
            gatk_threads=int(os.environ.get("MINOS_REAL_THREADS", "2")),
            memory_gb=int(os.environ.get("MINOS_REAL_MEMORY_GB", "4")),
        )
    )
    token = coord.issue_token("local")
    settings = WorkerSettings(
        coordinator_url="http://coordinator",
        worker_token=token,
        worker_id="local",
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "work",
        max_concurrent_jobs=1,
        execution_mode="official",
        scoring_version=version,
        log_level="INFO",
        cache_max_bytes=50 * 1024**3,
        max_memory_gb=64,
        max_timeout_seconds=7200,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        register(client, settings)
        assert run_once(client, settings, wait_seconds=0) is True
    view = coord.get_experiment("real-one")
    assert view["status"] == "COMPLETED", view
    assert view["best_score_source"] == "official"
    assert view["minos_commit"] == head_commit()
    assert view["best_objective"] is not None
    text = coord.export_conf("real-one")
    assert "scoring_version=" + version in text
    # The number is whatever AdvancedScorer returned on this machine. Do not treat it as a benchmark.
    print(f"official_score={view['best_objective']} scoring_version={version} commit={head_commit()}")
