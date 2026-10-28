"""Coordinator tables. Workers never open this database."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkerToken(Base):
    __tablename__ = "worker_tokens"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(256), nullable=False)
    host_os: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    cpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    free_disk_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_parallel_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    docker_ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gatk_image: Mapped[str] = mapped_column(String(256), nullable=False)
    gatk_image_id: Mapped[str | None] = mapped_column(String(128))
    happy_image: Mapped[str] = mapped_column(String(256), nullable=False)
    scorer_version: Mapped[str] = mapped_column(String(8), nullable=False)
    scorer_name: Mapped[str] = mapped_column(String(32), nullable=False)
    minos_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cached_datasets: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    ram_used_mb: Mapped[int | None] = mapped_column(Integer)
    disk_total_mb: Mapped[int | None] = mapped_column(Integer)
    last_heartbeat: Mapped[str | None] = mapped_column(String(40))
    scheduling_mode: Mapped[str] = mapped_column(String(16), default="enabled", nullable=False)
    policy_max_parallel: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    chromosome: Mapped[str | None] = mapped_column(String(16))
    reference_build: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_header: Mapped[str] = mapped_column(String(256), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(Text, default="", nullable=False)
    license_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class DatasetFile(Base):
    __tablename__ = "dataset_files"
    __table_args__ = (
        UniqueConstraint("dataset_id", "relative_path", name="uq_dataset_file_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    search_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    search_space_json: Mapped[str] = mapped_column(Text, nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(8), nullable=False)
    scorer_name: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    budget: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    variability_penalty: Mapped[float] = mapped_column(Float, nullable=False)
    minos_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    gatk_image: Mapped[str] = mapped_column(String(256), nullable=False)
    gatk_image_id: Mapped[str] = mapped_column(String(128), nullable=False)
    happy_image: Mapped[str] = mapped_column(String(256), nullable=False)
    bcftools_image: Mapped[str] = mapped_column(String(256), nullable=False)
    training_dataset_ids: Mapped[str] = mapped_column(Text, nullable=False)
    held_out_dataset_ids: Mapped[str] = mapped_column(Text, nullable=False)
    top_n: Mapped[int] = mapped_column(Integer, nullable=False)
    max_parallel_global: Mapped[int] = mapped_column(Integer, nullable=False)
    job_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    gatk_threads: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    min_free_disk_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_enqueued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resume_status: Mapped[str | None] = mapped_column(String(16))
    selection_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class Trial(Base):
    __tablename__ = "trials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True, nullable=False)
    optuna_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    full_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    objective: Mapped[float | None] = mapped_column(Float)
    score_source: Mapped[str | None] = mapped_column(String(16))
    told: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("trial_id", "dataset_id", "role", name="uq_job_trial_dataset_role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True, nullable=False)
    trial_id: Mapped[str] = mapped_column(ForeignKey("trials.id"), index=True, nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    counts_toward_objective: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(40))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    threads: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    official_score: Mapped[float | None] = mapped_column(Float)
    mock_objective: Mapped[float | None] = mapped_column(Float)
    score_source: Mapped[str | None] = mapped_column(String(16))
    metrics_json: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    provenance_json: Mapped[str | None] = mapped_column(Text)
    log_excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class EvalCache(Base):
    __tablename__ = "eval_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    official_score: Mapped[float | None] = mapped_column(Float)
    mock_objective: Mapped[float | None] = mapped_column(Float)
    score_source: Mapped[str] = mapped_column(String(16), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class JobEvent(Base):
    """Observed job state change. Rows are written when the scheduler changes state."""

    __tablename__ = "job_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    at: Mapped[str] = mapped_column(String(40), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    at: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)


class ExportRecord(Base):
    __tablename__ = "export_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    config_text: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    research_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warning: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
