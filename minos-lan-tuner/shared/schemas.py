"""HTTP bodies for the coordinator API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkerRegister(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    hostname: str
    host_os: str
    platform: str
    cpu_count: int = Field(ge=1)
    ram_mb: int = Field(ge=1)
    free_disk_mb: int = Field(ge=0)
    max_parallel_jobs: int = Field(ge=1, le=64)
    docker_ready: bool
    gatk_image: str
    gatk_image_id: str | None = None
    happy_image: str
    scorer_version: Literal["v1", "v2"]
    scorer_name: str
    minos_commit: str
    execution_mode: Literal["mock", "official"]


class Heartbeat(BaseModel):
    active_job_ids: list[str] = Field(default_factory=list)
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    ram_used_mb: int | None = Field(default=None, ge=0)
    free_disk_mb: int | None = Field(default=None, ge=0)
    disk_total_mb: int | None = Field(default=None, ge=0)
    cached_dataset_ids: list[str] = Field(default_factory=list)
    log_excerpt: str | None = Field(default=None, max_length=8000)


class ClaimRequest(BaseModel):
    cached_dataset_ids: list[str] = Field(default_factory=list)
    execution_mode: Literal["mock", "official"]
    free_disk_mb: int = Field(ge=0)
    running_jobs: int = Field(ge=0)


class CompleteRequest(BaseModel):
    lease_token: str
    config_sha256: str
    score_source: Literal["official", "mock"]
    official_score: float | None = None
    mock_objective: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float = Field(ge=0)
    provenance: dict[str, Any] = Field(default_factory=dict)
    variant_count: int | None = Field(default=None, ge=0)


class FailRequest(BaseModel):
    lease_token: str
    error_class: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=4000)
    retryable: bool
    elapsed_seconds: float | None = Field(default=None, ge=0)
    log_excerpt: str | None = Field(default=None, max_length=8000)
    details: dict[str, Any] = Field(default_factory=dict)
    # Gzip or plain-text hap.py VCF, only when this worker cannot import pysam.
    happy_vcf_b64: str | None = Field(default=None, max_length=12_000_000)


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    search_profile: str = "stage1"
    search_space: dict[str, Any] | None = None
    trials: int = Field(ge=1, le=10000)
    seed: int = 1
    scoring_version: Literal["v1", "v2"]
    training_dataset_ids: list[str] = Field(min_length=1)
    held_out_dataset_ids: list[str] = Field(default_factory=list)
    variability_penalty: float = Field(default=0.0, ge=0)
    execution_mode: Literal["mock", "official"] = "official"
    top_n_validate: int = Field(default=3, ge=1, le=50)
    max_parallel_global: int = Field(default=8, ge=1, le=256)
    job_timeout_seconds: int = Field(default=3600, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=10)
    gatk_threads: int = Field(default=1, ge=1)
    memory_gb: int = Field(default=4, ge=1)
    min_free_disk_mb: int = Field(default=1024, ge=0)
    start: bool = True


class TokenIssue(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReleaseRequest(BaseModel):
    lease_token: str
    message: str = Field(default="worker released the lease", max_length=4000)


class DatasetImport(BaseModel):
    manifest: dict[str, Any]


class PracticeImport(BaseModel):
    sample_path: str = Field(min_length=1, max_length=1024)
    scoring_version: Literal["v1", "v2"]


class SampleImport(BaseModel):
    sample_id: str = Field(min_length=1, max_length=80)
    scoring_version: Literal["v1", "v2"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class WorkerPolicyPatch(BaseModel):
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=64)
    reset: bool = False
