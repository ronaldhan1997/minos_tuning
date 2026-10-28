"""Execution engines. Official and mock stay separate."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from integrations.minos.pin import ensure_import_paths
from integrations.minos.scoring import OfficialScore, run_happy, score_metrics


@dataclass
class EngineResult:
    ok: bool
    retryable: bool
    error_class: str | None = None
    message: str | None = None
    official_score: float | None = None
    mock_objective: float | None = None
    score_source: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    variant_count: int | None = None
    elapsed_seconds: float | None = None
    log_excerpt: str | None = None


def mock_objective(config_sha256: str, dataset_id: str) -> float:
    """Deterministic stand-in used only when execution_mode is mock.

    The number is not an AdvancedScorer result and must not be labeled as one.
    """
    digest = hashlib.sha256(f"{config_sha256}:{dataset_id}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:4], "big") / 2**32
    return 20.0 + fraction * 60.0


def run_mock(*, config_sha256: str, dataset_id: str) -> EngineResult:
    return EngineResult(
        ok=True,
        retryable=False,
        mock_objective=mock_objective(config_sha256, dataset_id),
        score_source="mock",
        message="mock execution; official_score is null",
    )


def run_official(
    *,
    bam: Path,
    reference: Path,
    output_vcf: Path,
    region: str,
    gatk_options: dict[str, Any],
    threads: int,
    memory_gb: int,
    timeout_seconds: int,
    truth_vcf: Path,
    sdf: Path,
    mutations_vcf: Path | None,
    scoring_version: str,
    stop_event: threading.Event | None = None,
    reap: Callable[[], None] | None = None,
) -> EngineResult:
    """Run ``templates.gatk.variant_call`` and then the official scorer.

    The worker opens one Minos container round so ``reap_live_containers`` can
    stop a container if the agent is asked to shut down. The GATK arguments
    themselves are built only inside the official template.
    """
    ensure_import_paths()
    from templates._common import begin_round, reap_live_containers
    from templates.gatk import variant_call
    from templates.tool_params import validate_and_build_flags

    validation = validate_and_build_flags("gatk", gatk_options)
    if not validation["valid"]:
        return EngineResult(
            ok=False,
            retryable=False,
            error_class="invalid_config",
            message="; ".join(validation["errors"]),
        )
    if not sdf.is_dir():
        return EngineResult(
            ok=False,
            retryable=False,
            error_class="scoring_inputs",
            message=f"RTG SDF directory is missing: {sdf.name}",
        )

    done = threading.Event()

    def _watch() -> None:
        while not done.wait(0.5):
            if stop_event is not None and stop_event.is_set():
                (reap or reap_live_containers)()
                return

    watcher = threading.Thread(target=_watch, name="minos-lan-reap", daemon=True)
    watcher.start()
    begin_round()
    try:
        called = variant_call(
            bam_path=bam,
            reference_path=reference,
            output_vcf_path=output_vcf,
            region=region,
            config={
                "gatk_options": gatk_options,
                "timeout": timeout_seconds,
                "threads": threads,
                "memory_gb": memory_gb,
                "ref_build": "GRCh38",
            },
        )
    except Exception as exc:  # noqa: BLE001 - template failures are job failures
        return EngineResult(
            ok=False,
            retryable=True,
            error_class="gatk_failed",
            message=str(exc),
        )
    finally:
        done.set()
        watcher.join(timeout=2)
        if stop_event is not None and stop_event.is_set():
            (reap or reap_live_containers)()

    if stop_event is not None and stop_event.is_set():
        return EngineResult(
            ok=False,
            retryable=True,
            error_class="cancelled",
            message="worker stopped during GATK",
        )
    if not called.get("success"):
        error = str(called.get("error") or "GATK failed")
        timed_out = error.lower().startswith("timeout")
        # The same arguments crash again. A second attempt only repeats it.
        graph_crash = "Graph must have ref source and sink vertices" in error
        return EngineResult(
            ok=False,
            retryable=not graph_crash,
            error_class="timeout" if timed_out else "gatk_failed",
            message=error,
            log_excerpt=error[:2000],
        )
    if not output_vcf.exists():
        return EngineResult(
            ok=False,
            retryable=True,
            error_class="gatk_failed",
            message="VCF was not created",
        )

    metrics = run_happy(
        truth_vcf=str(truth_vcf),
        query_vcf=str(output_vcf),
        reference_fasta=str(reference),
        region=region,
        reference_sdf=str(sdf),
        mutations_vcf=str(mutations_vcf) if mutations_vcf else None,
    )
    scored: OfficialScore = score_metrics(
        metrics,
        scoring_version=scoring_version,
        truth_vcf=str(truth_vcf),
        happy_vcf=None if not metrics else metrics.get("happy_vcf_path"),
    )
    if not scored.ok or scored.official_score is None:
        details = dict(scored.metrics)
        if scored.rejected_score is not None:
            details["discarded_official_score"] = scored.rejected_score
            details["discard_reason"] = scored.failure_reason
        return EngineResult(
            ok=False,
            retryable=False,
            error_class=scored.error_class or "scoring_failed",
            message=scored.failure_reason or "scoring failed",
            metrics=details,
            variant_count=int(called.get("variant_count") or 0),
        )
    runtime = None
    metadata = called.get("metadata") or {}
    if metadata.get("runtime_seconds") is not None:
        runtime = float(metadata["runtime_seconds"])
    return EngineResult(
        ok=True,
        retryable=False,
        official_score=scored.official_score,
        score_source="official",
        metrics=scored.metrics,
        variant_count=int(called.get("variant_count") or 0),
        elapsed_seconds=runtime,
    )
