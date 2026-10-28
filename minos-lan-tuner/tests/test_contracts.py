"""Contracts copied from the pinned Minos checkout, plus path and score gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from integrations.minos.pin import PINNED_COMMIT, commit_matches_pin, head_commit
from integrations.minos.scoring import recording_decision, score_metrics
from integrations.minos.search import SearchSpaceError, full_config, load_search_space, parse_conf_text
from shared.docker_paths import to_docker_volume_arg
from shared.hashing import canonical_json, evaluation_cache_key
from utils.scoring import AdvancedScorer


def test_checkout_is_the_pinned_commit() -> None:
    assert head_commit() == PINNED_COMMIT
    assert commit_matches_pin()


def test_frozen_client_reports_stamped_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("integrations.minos.pin.sys.frozen", True, raising=False)
    monkeypatch.setenv("MINOS_COMMIT", "deadbeef")
    assert head_commit() == "deadbeef"


def test_windows_client_rejects_a_blank_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.worker.settings import SettingsError
    from apps.worker.windows_client import build_config, readiness_message

    with pytest.raises(SettingsError):
        build_config("192.168.1.20", "token", "laptop-a")
    monkeypatch.setenv("COMPUTERNAME", "OFFICE-PC")
    values = build_config("http://192.168.1.20:8091/", " token ", "")
    assert values["coordinator_url"] == "http://192.168.1.20:8091"
    assert values["worker_token"] == "token"
    assert values["worker_id"] == "OFFICE-PC"
    assert readiness_message({"ok": True, "checks": [{"name": "coordinator", "ok": False}]}) == (
        "The server did not answer. Check the address. Use the Ubuntu machine's address, not 127.0.0.1."
    )
    assert readiness_message({"ok": True, "checks": [{"name": "coordinator", "ok": True}]}) is None


def test_windows_client_saves_the_server_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.worker.windows_client import apply_config, load_config, save_config

    monkeypatch.setenv("MINOS_CLIENT_HOME", str(tmp_path))
    save_config(
        {
            "coordinator_url": "http://192.168.1.20:8091",
            "worker_token": "token-value",
            "worker_id": "laptop-a",
            "scoring_version": "v1",
            "execution_mode": "mock",
            "max_concurrent_jobs": "1",
        }
    )
    loaded = load_config()
    assert loaded is not None
    settings = apply_config(loaded)
    assert settings.coordinator_url == "http://192.168.1.20:8091"
    assert settings.worker_id == "laptop-a"
    assert settings.execution_mode == "mock"
    assert settings.cache_dir == tmp_path / "cache"


def test_search_space_matches_official_bounds() -> None:
    from integrations.minos.search import default_search_path

    stage1 = load_search_space(default_search_path(), "stage1")
    stage2 = load_search_space(default_search_path(), "stage2")
    assert "emit_ref_confidence" not in stage1.names()
    assert set(stage1.names()) <= set(stage2.names())
    assert "min_pruning" in stage1.names()
    assert "pcr_indel_model" in stage1.names()
    assert "max_num_haplotypes_in_population" in stage2.names()


def test_unknown_parameter_and_emit_ref_are_rejected() -> None:
    from integrations.minos.search import parse_search_parameters

    with pytest.raises(SearchSpaceError):
        parse_search_parameters({"not_a_gatk_flag": {"type": "int", "low": 0, "high": 1}}, profile="bad")
    with pytest.raises(SearchSpaceError):
        parse_search_parameters(
            {"emit_ref_confidence": {"type": "enum", "choices": ["NONE"]}},
            profile="bad",
        )


def test_conf_round_trip_uses_the_official_parser() -> None:
    from integrations.minos.search import dump_conf
    from templates.tool_params import validate_and_build_flags

    config = full_config({})
    text = dump_conf(config, ["provenance"])
    parsed = parse_conf_text(text)
    result = validate_and_build_flags("gatk", parsed)
    assert result["valid"], result["errors"]
    assert parsed["emit_ref_confidence"] == "NONE"
    assert parsed["min_pruning"] == 2


def test_v1_score_is_the_official_function_and_v2_does_not_fall_back() -> None:
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
    }
    scored = score_metrics(metrics, scoring_version="v1")
    assert scored.ok
    assert scored.official_score == pytest.approx(AdvancedScorer.compute_advanced_score(metrics))
    assert scored.scorer_name == "Advanced"
    refused = score_metrics(metrics, scoring_version="v2")
    assert not refused.ok
    assert refused.official_score is None
    assert refused.error_class == "scoring_unavailable"
    assert refused.scorer_name == "AdvancedV2"


def test_v2_names_a_missing_pysam(monkeypatch) -> None:
    monkeypatch.setattr("integrations.minos.scoring._pysam_importable", lambda: False)
    refused = score_metrics({"f1_snp": 0.9, "f1_indel": 0.9}, scoring_version="v2")
    assert refused.error_class == "scoring_unavailable"
    assert "pysam" in (refused.failure_reason or "")


def test_validator_fingerprint_is_not_a_usable_objective() -> None:
    assert recording_decision(0.25, {"f1_snp": 0.0, "f1_indel": 0.0})
    assert recording_decision(0.0, {"f1_snp": 0.2, "f1_indel": 0.2}) is None
    assert recording_decision(0.9, {"f1_snp": 0.0, "f1_indel": 0.0}) is None


def test_windows_docker_mount_conversion() -> None:
    assert to_docker_volume_arg(r"C:\Program Files\Minos Data\bam", "win32") == "/c/Program Files/Minos Data/bam"
    assert to_docker_volume_arg("/data/cache/sample", "linux") == "/data/cache/sample"
    with pytest.raises(ValueError):
        to_docker_volume_arg(r"\\server\share\bam", "win32")
    with pytest.raises(ValueError):
        to_docker_volume_arg("", "linux")


def test_cache_key_changes_when_the_scorer_changes() -> None:
    base = dict(
        config_sha256="a" * 64,
        manifest_sha256="b" * 64,
        reference_sha256="c" * 64,
        minos_commit="deadbeef",
        scoring_version="v1",
        gatk_image="broadinstitute/gatk:4.5.0.0",
        gatk_image_id="sha256:abc",
        happy_image="happy",
        bcftools_image="bcf",
        threads=1,
        memory_gb=4,
        execution_mode="official",
    )
    first = evaluation_cache_key(**base)
    second = evaluation_cache_key(**{**base, "scoring_version": "v2"})
    mock = evaluation_cache_key(**{**base, "execution_mode": "mock"})
    assert first != second
    assert first != mock
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_manifest_rejects_a_grch37_header(tmp_path: Path) -> None:
    from datasets.manifest import ManifestError, load_manifest_document
    from tests.conftest import synthetic_manifest

    root = tmp_path / "sample"
    root.mkdir()
    document = synthetic_manifest(root, "sample-a")
    reference = next(item for item in document["files"] if item["role"] == "reference")
    Path(reference["path"]).write_text(">hg19 chr20\nAC\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest_document(document)


def test_official_engine_calls_the_template_and_scorer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from integrations.minos.engine import run_official

    seen: dict[str, object] = {}

    def variant_call(**kwargs):
        seen["call"] = kwargs
        kwargs["output_vcf_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_vcf_path"].write_bytes(b"##fileformat=VCFv4.2\n")
        return {"success": True, "variant_count": 3, "metadata": {"runtime_seconds": 1.5}}

    def score_vcf(self, **kwargs):
        seen["score"] = kwargs
        return {
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
        }

    monkeypatch.setattr("templates.gatk.variant_call", variant_call)
    monkeypatch.setattr("utils.scoring.HappyScorer.score_vcf", score_vcf)
    sdf = tmp_path / "sdf"
    sdf.mkdir()
    result = run_official(
        bam=tmp_path / "input.bam",
        reference=tmp_path / "ref.fa",
        output_vcf=tmp_path / "out" / "output.vcf.gz",
        region="chr20:1000-2000",
        gatk_options=full_config({}),
        threads=1,
        memory_gb=4,
        timeout_seconds=30,
        truth_vcf=tmp_path / "truth.vcf.gz",
        sdf=sdf,
        mutations_vcf=tmp_path / "mutations.vcf.gz",
        scoring_version="v1",
    )
    assert result.ok
    assert result.score_source == "official"
    assert result.official_score is not None
    call = seen["call"]
    assert call["config"]["threads"] == 1
    assert call["config"]["memory_gb"] == 4
    assert call["config"]["timeout"] == 30
    assert call["config"]["ref_build"] == "GRCh38"
    assert call["region"] == "chr20:1000-2000"
    assert seen["score"]["region"] == "chr20:1000-2000"
    assert seen["score"]["confident_bed"] is None
