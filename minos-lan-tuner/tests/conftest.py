"""Fixtures for the tuner. The parent Minos checkout stays on sys.path behind this project."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

def _checkout_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "start-miner.sh").is_file() and (candidate / "templates").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]


os.environ.setdefault("MINOS_REPO_ROOT", str(_checkout_root()))

from integrations.minos.pin import ensure_import_paths

ensure_import_paths()


@pytest.fixture
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path}/tuner.sqlite3"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("COORDINATOR_URL", raising=False)
    return url


def synthetic_manifest(root: Path, dataset_id: str, *, scoring_version: str = "v1") -> dict:
    bam = root / "input.bam"
    bam.write_bytes(b"BAM")
    bai = root / "input.bam.bai"
    bai.write_bytes(b"BAI")
    reference = root / "chr20.fa"
    reference.write_text(">chr20\nACGT\n", encoding="utf-8")
    index = root / "chr20.fa.fai"
    index.write_text("chr20\t4\t7\t4\t5\n", encoding="utf-8")
    truth = root / "truth.vcf.gz"
    truth.write_bytes(b"VCF")
    truth_index = root / "truth.vcf.gz.tbi"
    truth_index.write_bytes(b"TBI")
    mutations = root / "mutations.vcf.gz"
    mutations.write_bytes(b"MUT")
    mutations_index = root / "mutations.vcf.gz.tbi"
    mutations_index.write_bytes(b"MTBI")
    sdf = root / "chr20.sdf"
    sdf.mkdir()
    (sdf / "summary.txt").write_text("sdf", encoding="utf-8")
    files = [
        ("bam", bam),
        ("bai", bai),
        ("reference", reference),
        ("reference_index", index),
        ("truth", truth),
        ("truth_index", truth_index),
        ("mutations", mutations),
        ("mutations_index", mutations_index),
        ("sdf", sdf),
    ]
    return {
        "dataset_id": dataset_id,
        "name": dataset_id,
        "region": "chr20:1000-2000",
        "chromosome": "chr20",
        "reference_build": "GRCh38",
        "scoring_version": scoring_version,
        "source": "synthetic unit fixture",
        "license_note": "not a real genome",
        "files": [{"role": role, "path": str(path)} for role, path in files],
    }
