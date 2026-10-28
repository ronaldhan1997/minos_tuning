"""Call AdvancedScorer. Do not reimplement the formula."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from integrations.minos.pin import ensure_import_paths, scorer_name_for

# Keys HappyScorer may return. Missing keys stay null in the stored projection.
OFFICIAL_METRIC_KEYS = (
    "f1_snp",
    "f1_indel",
    "precision_snp",
    "precision_indel",
    "recall_snp",
    "recall_indel",
    "truth_total_snp",
    "truth_total_indel",
    "query_total_snp",
    "query_total_indel",
    "fp_snp",
    "fp_indel",
    "tp_snp",
    "tp_indel",
    "fn_snp",
    "fn_indel",
    "frac_na_snp",
    "frac_na_indel",
    "titv_truth_snp",
    "titv_query_snp",
    "hethom_truth_snp",
    "hethom_query_snp",
    "hethom_truth_indel",
    "hethom_query_indel",
    "weighted_f1",
    "fp_per_target",
    "overcall_penalty",
    "target_total_snp",
    "target_total_indel",
    "query_unk_snp",
    "query_unk_indel",
    "happy_vcf_path",
)


class OfficialScore(BaseModel):
    ok: bool
    scoring_version: str
    scorer_name: str
    official_score: float | None = None
    combined_final: float | None = None
    rejected_score: float | None = None
    failure_reason: str | None = None
    # scoring_unavailable: hap.py or v2 inputs missing.
    # not_recorded: a number exists but neurons/validator.py would drop it.
    # unsupported_version: not v1 or v2.
    error_class: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    score_source: str = "official"


def recording_decision(combined_final: float, metrics: dict[str, Any]) -> str | None:
    """Return a reason to drop the score, or None when a validator would keep it.

    The local-score call site in ``neurons/validator.py`` passes
    ``allow_zero=True`` to ``_valid_round_score``. The same file's
    ``_is_zero_input_advanced_fingerprint`` then drops the all-zero v1 band.
    """
    if not math.isfinite(combined_final) or combined_final < 0.0 or combined_final > 1.0:
        return "combined_final is outside [0, 1]"
    f1_snp = metrics.get("f1_snp") or 0.0
    f1_indel = metrics.get("f1_indel") or 0.0
    if f1_snp == 0.0 and f1_indel == 0.0 and 0.24999 <= combined_final <= 0.25001:
        return (
            "zero-input AdvancedScorer fingerprint; "
            "neurons/validator.py would not record this score"
        )
    return None


def project_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    projected = {key: None for key in OFFICIAL_METRIC_KEYS}
    if not metrics:
        return projected
    for key in OFFICIAL_METRIC_KEYS:
        if key in metrics and metrics[key] is not None:
            projected[key] = metrics[key]
    return projected


def score_metrics(
    metrics: dict[str, Any] | None,
    *,
    scoring_version: str,
    truth_vcf: str | None = None,
    happy_vcf: str | None = None,
) -> OfficialScore:
    """Apply the official scorer for one version.

    v2 does not fall back to v1. A missing annotated hap.py VCF leaves the v2
    score null. Crashes and null scores are not zeros.
    """
    ensure_import_paths()
    from utils.scoring import AdvancedScorer
    from utils.scoring_version import V1, V2, normalise

    version = normalise(scoring_version)
    name = scorer_name_for(version or "")
    projected = project_metrics(metrics)
    if version not in (V1, V2):
        return OfficialScore(
            ok=False,
            scoring_version=str(scoring_version),
            scorer_name=name,
            failure_reason=f"scoring version {scoring_version!r} is not v1 or v2",
            error_class="unsupported_version",
            metrics=projected,
        )
    if metrics is None:
        return OfficialScore(
            ok=False,
            scoring_version=version,
            scorer_name=name,
            failure_reason="hap.py returned no metrics",
            error_class="scoring_unavailable",
            metrics=projected,
        )

    if version == V2:
        if not _pysam_importable():
            return OfficialScore(
                ok=False,
                scoring_version=version,
                scorer_name=name,
                failure_reason=(
                    "v2 could not be computed because this app cannot import pysam, "
                    "which reads the hap.py VCF. The v1 formula is a different scale "
                    "and is not substituted."
                ),
                error_class="scoring_unavailable",
                metrics=projected,
            )
        numeric = _score_v2(metrics, truth_vcf, happy_vcf)
        if numeric is None:
            return OfficialScore(
                ok=False,
                scoring_version=version,
                scorer_name=name,
                failure_reason=(
                    "v2 could not be computed. The v1 formula is a different scale "
                    "and is not substituted."
                ),
                error_class="scoring_unavailable",
                metrics=projected,
            )
    else:
        numeric = float(AdvancedScorer.compute_advanced_score(metrics))

    if isinstance(numeric, bool) or not isinstance(numeric, (int, float)) or not math.isfinite(float(numeric)):
        return OfficialScore(
            ok=False,
            scoring_version=version,
            scorer_name=name,
            failure_reason=f"official score is not a finite number: {numeric!r}",
            error_class="scoring_unavailable",
            metrics=projected,
        )
    value = float(numeric)
    if not 0.0 <= value <= 100.0:
        return OfficialScore(
            ok=False,
            scoring_version=version,
            scorer_name=name,
            rejected_score=value,
            failure_reason=f"official score {value} is outside 0..100",
            error_class="not_recorded",
            metrics=projected,
        )
    combined = value / 100.0
    dropped = recording_decision(combined, metrics or {})
    if dropped:
        return OfficialScore(
            ok=False,
            scoring_version=version,
            scorer_name=name,
            rejected_score=value,
            combined_final=combined,
            failure_reason=dropped,
            error_class="not_recorded",
            metrics=projected,
        )
    return OfficialScore(
        ok=True,
        scoring_version=version,
        scorer_name=name,
        official_score=value,
        combined_final=combined,
        metrics=projected,
    )


def _pysam_importable() -> bool:
    try:
        import pysam  # noqa: F401
    except ImportError:
        return False
    return True


_HAPPY_VCF_LIMIT = 8 * 1024 * 1024


def score_uploaded_happy(metrics: dict[str, Any] | None, happy_vcf_b64: str) -> OfficialScore:
    """Score a hap.py VCF the worker uploaded because it has no pysam.

    The numeric result still comes from ``AdvancedScorer.compute_score_v2``.
    """
    import base64
    import os
    import tempfile

    name = scorer_name_for("v2")
    try:
        raw = base64.b64decode(happy_vcf_b64, validate=True)
    except (ValueError, TypeError):
        return OfficialScore(
            ok=False,
            scoring_version="v2",
            scorer_name=name,
            failure_reason="The uploaded hap.py VCF is not valid base64.",
            error_class="scoring_unavailable",
            metrics=project_metrics(metrics),
        )
    if not raw or len(raw) > _HAPPY_VCF_LIMIT:
        return OfficialScore(
            ok=False,
            scoring_version="v2",
            scorer_name=name,
            failure_reason="The uploaded hap.py VCF is empty or larger than 8 MiB.",
            error_class="scoring_unavailable",
            metrics=project_metrics(metrics),
        )
    suffix = ".vcf.gz" if raw[:2] == b"\x1f\x8b" else ".vcf"
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, raw)
        os.close(fd)
        fd = -1
        return score_metrics(metrics, scoring_version="v2", happy_vcf=path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


def _score_v2(
    metrics: dict[str, Any],
    truth_vcf: str | None,
    happy_vcf: str | None,
) -> float | None:
    from utils.scoring import AdvancedScorer, difficulty_class_counts, parse_happy_vcf

    path = happy_vcf or metrics.get("happy_vcf_path")
    if not path or not Path(str(path)).exists():
        return None
    records = parse_happy_vcf(str(path), truth_vcf_path=truth_vcf)
    if not records:
        return None
    return AdvancedScorer.compute_score_v2(metrics, difficulty_class_counts(records))


def run_happy(
    *,
    truth_vcf: str,
    query_vcf: str,
    reference_fasta: str,
    region: str,
    reference_sdf: str,
    mutations_vcf: str | None,
) -> dict[str, Any] | None:
    """Run ``HappyScorer.score_vcf`` with the arguments the miner uses."""
    ensure_import_paths()
    from utils.scoring import HappyScorer

    return HappyScorer().score_vcf(
        truth_vcf=truth_vcf,
        query_vcf=query_vcf,
        reference_fasta=reference_fasta,
        confident_bed=None,
        region=region,
        reference_sdf=reference_sdf,
        mutations_vcf=mutations_vcf,
    )
