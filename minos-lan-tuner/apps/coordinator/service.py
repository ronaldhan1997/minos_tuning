"""Scheduler, Optuna ask/tell, and dataset registry.

Workers never open this database. One coordinator process owns every
``study.ask`` and ``study.tell``. ``tell`` uses Optuna's ``skip_if_finished``
and a ``trials.told`` flag so a crash between the two stores is reconciled
on the next startup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import optuna
from optuna.samplers import TPESampler
from optuna.trial import TrialState
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.coordinator.clock import after, iso, parse_iso, utc_now
from apps.coordinator.errors import ServiceError
from apps.coordinator.objective import SELECTION_RULE, objective_value
from database.models import (
    AuditEvent,
    Dataset,
    DatasetFile,
    EvalCache,
    Experiment,
    ExportRecord,
    Job,
    JobEvent,
    Trial,
    Worker,
    WorkerToken,
)
from database.session import current_database_url, session
from datasets.manifest import ManifestError, from_practice_sample, load_manifest_document
from integrations.minos.pin import (
    bcftools_image,
    gatk_image,
    happy_image,
    head_commit,
    inspect_image_id,
    require_pin,
    scorer_name_for,
    source_dirty,
)
from integrations.minos.search import (
    SearchSpace,
    SearchSpaceError,
    baseline_params,
    default_search_path,
    dump_conf,
    full_config,
    load_search_space,
    parse_search_parameters,
    space_from_document,
    suggest,
)
from shared.hashing import config_sha256, evaluation_cache_key
from shared.resources import usable_resources
from shared.schemas import (
    ClaimRequest,
    CompleteRequest,
    ExperimentCreate,
    FailRequest,
    Heartbeat,
    ReleaseRequest,
    WorkerRegister,
)

log = logging.getLogger("minos.coordinator")

ACTIVE = ("PENDING", "LEASED", "RUNNING")
TERMINAL_JOB = ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED")
NON_RETRYABLE = {
    "invalid_config",
    "not_recorded",
    "unsupported_version",
    "scoring_unavailable",
    "incompatible_job",
}
SCHEDULING_MODES = {"enabled", "paused", "draining"}


def _note_job(db: Session, job: Job, state: str, note: str) -> None:
    db.add(
        JobEvent(
            id=str(uuid.uuid4()),
            job_id=job.id,
            experiment_id=job.experiment_id,
            state=state,
            note=note[:500],
            at=iso(),
        )
    )


def _audit(
    db: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    result: str,
    detail: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> None:
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            at=iso(),
            actor=actor[:128],
            action=action[:64],
            target_type=target_type[:32],
            target_id=target_id[:128],
            result=result[:16],
            detail_json=_dumps(detail or {}),
            idempotency_key=None if not idempotency_key else idempotency_key[:128],
        )
    )


def _replay(db: Session, key: str | None, action: str, target_id: str | None = None) -> AuditEvent | None:
    if not key:
        return None
    stmt = select(AuditEvent).where(
        AuditEvent.idempotency_key == key[:128],
        AuditEvent.action == action,
        AuditEvent.result == "ok",
    )
    if target_id is not None:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    return db.scalar(stmt.order_by(AuditEvent.at.desc()))


def effective_parallel(worker: Worker) -> int:
    cap = int(worker.max_parallel_jobs)
    if worker.policy_max_parallel is not None:
        cap = min(cap, int(worker.policy_max_parallel))
    return max(1, cap)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _loads(payload: str | None, default: Any) -> Any:
    if not payload:
        return default
    return json.loads(payload)


class Coordinator:
    def __init__(self, admin_token: str, lease_grace_seconds: int = 120) -> None:
        if lease_grace_seconds < 1:
            raise ValueError("lease_grace_seconds must be >= 1")
        self.admin_token = admin_token
        self.lease_grace_seconds = lease_grace_seconds
        self._lock = threading.Lock()
        self._storage: optuna.storages.RDBStorage | None = None

    @contextmanager
    def _db(self) -> Iterator[Session]:
        with self._lock:
            db = session()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def optuna_storage_url(self) -> str:
        """Postgres shares the coordinator database. SQLite uses a sidecar file.

        A single SQLite file cannot take the scheduler write transaction and an
        Optuna write at the same time. The sidecar is ``<database>.optuna``.
        """
        url = current_database_url()
        if not url.startswith("sqlite:///"):
            return url
        raw = url[len("sqlite:///") :]
        if raw in {":memory:", ""}:
            raise ServiceError(500, "database", "Optuna needs a file-backed SQLite database")
        sidecar = Path(raw).with_name(Path(raw).name + ".optuna")
        return "sqlite:///" + sidecar.resolve().as_posix()

    def storage(self) -> optuna.storages.RDBStorage:
        if self._storage is None:
            url = self.optuna_storage_url()
            kwargs: dict[str, Any] = {}
            if url.startswith("sqlite"):
                kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
            self._storage = optuna.storages.RDBStorage(url, engine_kwargs=kwargs)
        return self._storage

    def reconcile_all(self) -> None:
        with self._db() as db:
            experiments = list(db.scalars(select(Experiment)))
            now = utc_now()
            for exp in experiments:
                if exp.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    continue
                self._expire(db, now)
                if exp.status == "PAUSED":
                    self._finalize_open_trials(db, exp)
                else:
                    self._pump(db, exp)

    # --- auth ---

    def issue_token(self, worker_id: str, *, actor: str = "system") -> str:
        raw = secrets.token_urlsafe(32)
        with self._db() as db:
            row = db.get(WorkerToken, worker_id)
            replaced = row is not None
            if row is None:
                row = WorkerToken(
                    worker_id=worker_id,
                    token_hash=token_hash(raw),
                    revoked=False,
                    created_at=iso(),
                )
                db.add(row)
            else:
                row.token_hash = token_hash(raw)
                row.revoked = False
                row.created_at = iso()
            _audit(
                db,
                actor=actor,
                action="worker.token.issue",
                target_type="worker",
                target_id=worker_id,
                result="ok",
                detail={"replaced": replaced},
            )
        return raw

    def list_tokens(self) -> dict[str, Any]:
        with self._db() as db:
            rows = db.scalars(select(WorkerToken).order_by(WorkerToken.created_at.desc())).all()
            return {
                "items": [
                    {
                        "worker_id": row.worker_id,
                        "revoked": row.revoked,
                        "created_at": row.created_at,
                        "registered": db.get(Worker, row.worker_id) is not None,
                    }
                    for row in rows
                ]
            }

    def revoke_token(self, worker_id: str, *, actor: str = "system") -> dict[str, Any]:
        with self._db() as db:
            row = db.get(WorkerToken, worker_id)
            if row is None:
                raise ServiceError(404, "not_found", "no token exists for that computer")
            row.revoked = True
            _audit(
                db,
                actor=actor,
                action="worker.token.revoke",
                target_type="worker",
                target_id=worker_id,
                result="ok",
            )
            return {"worker_id": row.worker_id, "revoked": True, "created_at": row.created_at}

    def _worker_for_token(self, db: Session, raw_token: str) -> Worker:
        digest = token_hash(raw_token)
        row = db.scalar(select(WorkerToken).where(WorkerToken.token_hash == digest))
        if row is None or row.revoked:
            raise ServiceError(401, "unauthorized", "worker token was rejected")
        worker = db.get(Worker, row.worker_id)
        if worker is None or not hmac.compare_digest(worker.token_hash, digest):
            raise ServiceError(401, "unauthorized", "worker is not registered")
        return worker

    def worker_from_token(self, raw_token: str) -> str:
        with self._db() as db:
            return self._worker_for_token(db, raw_token).id

    def worker_config(self, raw_token: str) -> dict[str, Any]:
        """The running study a client should follow. The client does not choose the score."""
        digest = token_hash(raw_token)
        with self._db() as db:
            issued = db.scalar(select(WorkerToken).where(WorkerToken.token_hash == digest))
            if issued is None or issued.revoked:
                raise ServiceError(401, "unauthorized", "worker token was rejected")
            running = list(
                db.scalars(select(Experiment).where(Experiment.status.in_(("RUNNING", "VALIDATING"))))
            )
            chosen = _preferred_study(db, running)
            if chosen is None:
                return {
                    "available": False,
                    "execution_mode": None,
                    "scoring_version": None,
                    "scorer_name": None,
                    "experiment_name": None,
                }
            return {
                "available": True,
                "execution_mode": chosen.execution_mode,
                "scoring_version": chosen.scoring_version,
                "scorer_name": chosen.scorer_name,
                "experiment_name": chosen.name,
                "gatk_image": chosen.gatk_image,
                "gatk_image_id": chosen.gatk_image_id,
                "happy_image": chosen.happy_image,
                "bcftools_image": chosen.bcftools_image,
            }

    def require_admin_token(self, presented: str | None, *, loopback: bool) -> None:
        if loopback and _env_loopback_ok():
            return
        if not presented or not self.admin_token:
            raise ServiceError(401, "unauthorized", "admin token was rejected")
        if not hmac.compare_digest(presented, self.admin_token):
            raise ServiceError(401, "unauthorized", "admin token was rejected")

    # --- workers ---

    def register(self, body: WorkerRegister, raw_token: str) -> dict[str, Any]:
        digest = token_hash(raw_token)
        expected_name = scorer_name_for(body.scorer_version)
        if body.scorer_name != expected_name:
            raise ServiceError(
                400,
                "scorer_name",
                f"scorer_name {body.scorer_name!r} does not match {expected_name!r} for {body.scorer_version}",
            )
        pinned = head_commit()
        if body.minos_commit != pinned:
            raise ServiceError(
                400,
                "minos_commit",
                f"worker commit {body.minos_commit} does not match coordinator pin {pinned}",
            )
        if body.execution_mode == "official":
            if not body.docker_ready:
                raise ServiceError(400, "docker", "official workers must report docker_ready")
            if body.gatk_image != gatk_image() or body.happy_image != happy_image():
                raise ServiceError(400, "image", "worker images do not match the pinned Minos templates")
            if not body.gatk_image_id:
                raise ServiceError(400, "image", "official workers must report the GATK image id")
        with self._db() as db:
            issued = db.get(WorkerToken, body.worker_id)
            if issued is None or issued.revoked or not hmac.compare_digest(issued.token_hash, digest):
                raise ServiceError(401, "unauthorized", "worker token was rejected")
            now = iso()
            worker = db.get(Worker, body.worker_id)
            fields = body.model_dump()
            if worker is None:
                worker = Worker(
                    id=body.worker_id,
                    cached_datasets="[]",
                    token_hash=digest,
                    created_at=now,
                    updated_at=now,
                    last_heartbeat=now,
                    scheduling_mode="enabled",
                    **{key: fields[key] for key in fields if key != "worker_id"},
                )
                db.add(worker)
            else:
                for key, value in fields.items():
                    if key == "worker_id":
                        continue
                    setattr(worker, key, value)
                worker.token_hash = digest
                worker.updated_at = now
                worker.last_heartbeat = now
            return _worker_view(worker)

    def heartbeat(self, raw_token: str, body: Heartbeat) -> dict[str, Any]:
        with self._db() as db:
            worker = self._worker_for_token(db, raw_token)
            now = utc_now()
            worker.last_heartbeat = iso(now)
            worker.updated_at = iso(now)
            worker.cached_datasets = _dumps(body.cached_dataset_ids)
            if body.free_disk_mb is not None:
                worker.free_disk_mb = body.free_disk_mb
            if body.disk_total_mb is not None:
                worker.disk_total_mb = body.disk_total_mb
            if body.cpu_percent is not None:
                worker.cpu_percent = body.cpu_percent
            if body.ram_used_mb is not None:
                worker.ram_used_mb = body.ram_used_mb
            cancelled: list[str] = []
            for job_id in body.active_job_ids:
                job = db.get(Job, job_id)
                if job is None or job.worker_id != worker.id:
                    continue
                exp = db.get(Experiment, job.experiment_id)
                if job.state == "CANCELLED" or (exp is not None and exp.status == "CANCELLED"):
                    cancelled.append(job.id)
                    continue
                if job.state == "LEASED" and job.lease_token:
                    job.state = "RUNNING"
                    job.lease_expires_at = after(job.timeout_seconds + self.lease_grace_seconds, now)
                    job.updated_at = iso(now)
                    _note_job(db, job, "RUNNING", "worker heartbeat")
                elif job.state == "RUNNING" and job.lease_token:
                    job.lease_expires_at = after(job.timeout_seconds + self.lease_grace_seconds, now)
                    job.updated_at = iso(now)
                if body.log_excerpt and job.state == "RUNNING" and len(body.active_job_ids) == 1:
                    job.log_excerpt = body.log_excerpt[-8000:]
            self._release_unreported_jobs(db, worker.id, set(body.active_job_ids), now)
            self._expire(db, now)
            self._finalize_touched(db)
            return {"ok": True, "cancelled_job_ids": cancelled, "server_time": iso(now)}

    def _release_unreported_jobs(self, db: Session, worker_id: str, reported: set[str], now: Any) -> None:
        """A check-in that leaves out a job means this computer is not running it.

        The dashboard otherwise stays on Working for a job the client already
        dropped. A job touched in the last minute is kept, so a check-in that
        races the start of work does not take the job away.
        """
        owned = list(
            db.scalars(select(Job).where(Job.worker_id == worker_id, Job.state.in_(("LEASED", "RUNNING"))))
        )
        for job in owned:
            if job.id in reported:
                continue
            if (now - parse_iso(job.updated_at)).total_seconds() < 60:
                continue
            exp = db.get(Experiment, job.experiment_id)
            if exp is not None and exp.status not in {"RUNNING", "VALIDATING"}:
                continue
            job.state = "PENDING"
            job.lease_token = None
            job.worker_id = None
            job.lease_expires_at = None
            job.error_class = "abandoned"
            job.error_message = "the computer checked in without this job"
            job.updated_at = iso(now)
            _note_job(db, job, "PENDING", "client checked in without this job")

    def claim(self, raw_token: str, body: ClaimRequest, wait_seconds: int = 0) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            with self._db() as db:
                worker = self._worker_for_token(db, raw_token)
                self._expire(db, utc_now())
                payload = self._claim_once(db, worker, body)
                if payload is not None:
                    return payload
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def complete(self, raw_token: str, job_id: str, body: CompleteRequest) -> dict[str, Any]:
        with self._db() as db:
            worker = self._worker_for_token(db, raw_token)
            job = self._require_job(db, job_id)
            if job.state == "COMPLETED" and job.lease_token == body.lease_token and job.worker_id == worker.id:
                return {"job_id": job.id, "state": job.state, "idempotent": True}
            self._require_active_lease(job, worker.id, body.lease_token)
            exp = self._require_experiment(db, job.experiment_id)
            self._check_score_body(exp, body)
            if body.config_sha256 != job.config_sha256:
                raise ServiceError(400, "config_hash", "config hash does not match the leased job")
            now = iso()
            job.state = "COMPLETED"
            job.official_score = body.official_score
            job.mock_objective = body.mock_objective
            job.score_source = body.score_source
            job.metrics_json = _dumps(body.metrics)
            job.elapsed_seconds = body.elapsed_seconds
            provenance = dict(body.provenance)
            provenance.update(
                {
                    "worker_id": worker.id,
                    "host_os": worker.host_os,
                    "platform": worker.platform,
                    "minos_commit": exp.minos_commit,
                    "scoring_version": exp.scoring_version,
                    "scorer_name": exp.scorer_name,
                    "gatk_image": exp.gatk_image,
                    "gatk_image_id": exp.gatk_image_id,
                    "variant_count": body.variant_count,
                }
            )
            job.provenance_json = _dumps(provenance)
            job.updated_at = now
            job.error_class = None
            job.error_message = None
            _note_job(db, job, "COMPLETED", "worker reported a score")
            self._remember_cache(db, job, exp)
            self._pump(db, exp)
            return {"job_id": job.id, "state": job.state, "idempotent": False, "experiment_status": exp.status}

    def fail(self, raw_token: str, job_id: str, body: FailRequest) -> dict[str, Any]:
        rescued = None
        if body.error_class == "scoring_unavailable" and body.happy_vcf_b64:
            from integrations.minos.scoring import score_uploaded_happy

            rescued = score_uploaded_happy(body.details, body.happy_vcf_b64)
        with self._db() as db:
            worker = self._worker_for_token(db, raw_token)
            job = self._require_job(db, job_id)
            if (
                job.state in {"FAILED", "TIMED_OUT", "CANCELLED"}
                and job.worker_id == worker.id
                and job.lease_token == body.lease_token
            ):
                return {"job_id": job.id, "state": job.state, "idempotent": True}
            self._require_active_lease(job, worker.id, body.lease_token)
            exp = self._require_experiment(db, job.experiment_id)
            if (
                rescued is not None
                and rescued.ok
                and rescued.official_score is not None
                and exp.execution_mode == "official"
                and exp.scoring_version == "v2"
            ):
                now = iso()
                job.state = "COMPLETED"
                job.official_score = rescued.official_score
                job.mock_objective = None
                job.score_source = "official"
                job.metrics_json = _dumps(rescued.metrics)
                job.elapsed_seconds = body.elapsed_seconds
                job.error_class = None
                job.error_message = None
                job.provenance_json = _dumps(
                    {
                        "worker_id": worker.id,
                        "host_os": worker.host_os,
                        "platform": worker.platform,
                        "minos_commit": exp.minos_commit,
                        "scoring_version": exp.scoring_version,
                        "scorer_name": exp.scorer_name,
                        "gatk_image": exp.gatk_image,
                        "gatk_image_id": exp.gatk_image_id,
                        "scored_on": "coordinator",
                        "reason": "worker has no pysam; the hap.py VCF was scored here",
                    }
                )
                job.updated_at = now
                _note_job(db, job, "COMPLETED", "coordinator scored the uploaded hap.py VCF")
                self._remember_cache(db, job, exp)
                self._pump(db, exp)
                return {
                    "job_id": job.id,
                    "state": job.state,
                    "idempotent": False,
                    "experiment_status": exp.status,
                }
            now = iso()
            message = body.message
            if rescued is not None and not rescued.ok and rescued.failure_reason:
                message = rescued.failure_reason
            job.error_class = body.error_class
            job.error_message = message[:4000]
            job.elapsed_seconds = body.elapsed_seconds
            job.metrics_json = _dumps(body.details)
            if body.log_excerpt:
                job.log_excerpt = body.log_excerpt[:8000]
            job.updated_at = now
            retryable = body.retryable and body.error_class not in NON_RETRYABLE
            if retryable and job.attempt < job.max_attempts and exp.status not in {"CANCELLED", "PAUSED"}:
                job.attempt += 1
                job.state = "PENDING"
                job.lease_token = None
                job.worker_id = None
                job.lease_expires_at = None
            else:
                job.state = "TIMED_OUT" if body.error_class == "timeout" else "FAILED"
            _note_job(db, job, job.state, body.error_class)
            self._pump(db, exp)
            return {"job_id": job.id, "state": job.state, "idempotent": False, "attempt": job.attempt}

    def release(self, raw_token: str, job_id: str, body: ReleaseRequest) -> dict[str, Any]:
        with self._db() as db:
            worker = self._worker_for_token(db, raw_token)
            job = self._require_job(db, job_id)
            self._require_active_lease(job, worker.id, body.lease_token)
            job.state = "PENDING"
            job.lease_token = None
            job.worker_id = None
            job.lease_expires_at = None
            job.error_class = "released"
            job.error_message = body.message
            job.updated_at = iso()
            _note_job(db, job, "PENDING", "worker released the lease")
            return {"job_id": job.id, "state": job.state}

    # --- datasets ---

    def import_document(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            prepared = load_manifest_document(raw)
        except ManifestError as exc:
            raise ServiceError(400, "manifest", str(exc)) from exc
        return self._store_manifest(prepared)

    def import_practice(self, sample_json: Path, scoring_version: str) -> dict[str, Any]:
        try:
            prepared = from_practice_sample(
                sample_json,
                scoring_version=scoring_version,
                role_note="Local Minos practice sample.json. Not uploaded.",
            )
        except (ManifestError, OSError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "manifest", str(exc)) from exc
        return self._store_manifest(prepared)

    def delete_dataset(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.get(Dataset, dataset_id)
            if row is None:
                raise ServiceError(404, "not_found", "dataset was not found")
            used = db.scalar(select(func.count()).select_from(Job).where(Job.dataset_id == dataset_id))
            if used:
                raise ServiceError(409, "dataset_in_use", "dataset is referenced by jobs")
            for item in db.scalars(select(DatasetFile).where(DatasetFile.dataset_id == dataset_id)):
                db.delete(item)
            db.delete(row)
            return {"dataset_id": dataset_id, "deleted": True}

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = list(db.scalars(select(Dataset).order_by(Dataset.created_at)))
            return [_dataset_view(row) for row in rows]

    def manifest(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            dataset = db.get(Dataset, dataset_id)
            if dataset is None:
                raise ServiceError(404, "not_found", "dataset was not found")
            files = list(
                db.scalars(
                    select(DatasetFile)
                    .where(DatasetFile.dataset_id == dataset_id)
                    .order_by(DatasetFile.relative_path)
                )
            )
            return _public_manifest(dataset, files)

    def blob(self, dataset_id: str, sha256: str) -> tuple[Path, int]:
        with self._db() as db:
            row = db.scalar(
                select(DatasetFile).where(
                    DatasetFile.dataset_id == dataset_id,
                    DatasetFile.sha256 == sha256,
                )
            )
            if row is None:
                raise ServiceError(404, "not_found", "blob was not found")
            path = Path(row.local_path)
            if not path.is_file():
                raise ServiceError(404, "missing_file", "imported file is no longer on the coordinator disk")
            return path, int(row.size_bytes)

    # --- experiments ---

    def create_experiment(
        self,
        body: ExperimentCreate,
        *,
        actor: str = "system",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            with self._db() as db:
                prior = _replay(db, idempotency_key, "experiment.create")
                if prior is not None:
                    return self._experiment_view(db, self._by_key(db, prior.target_id))
        require_pin()
        if body.execution_mode == "official" and source_dirty():
            raise ServiceError(
                409,
                "dirty_source",
                "Pinned Minos source paths differ from HEAD. Official studies are refused.",
            )
        try:
            space = self._resolve_space(body)
        except SearchSpaceError as exc:
            raise ServiceError(400, "search_space", str(exc)) from exc
        overlap = set(body.training_dataset_ids) & set(body.held_out_dataset_ids)
        if overlap:
            raise ServiceError(400, "datasets", f"training and held-out overlap: {sorted(overlap)}")
        if body.execution_mode == "official":
            image_id = inspect_image_id(gatk_image())
            if not image_id:
                raise ServiceError(
                    409,
                    "image_missing",
                    f"Docker image {gatk_image()} is not present on the coordinator. "
                    "Pull it before an official study, or create the study in mock mode.",
                )
            for image in (happy_image(), bcftools_image()):
                if inspect_image_id(image) is None:
                    raise ServiceError(409, "image_missing", f"Docker image {image} is not present on the coordinator")
        else:
            image_id = "mock"
        with self._db() as db:
            if db.scalar(select(Experiment).where(Experiment.name == body.name)) is not None:
                raise ServiceError(409, "name_taken", f"experiment {body.name!r} already exists")
            for dataset_id in [*body.training_dataset_ids, *body.held_out_dataset_ids]:
                dataset = db.get(Dataset, dataset_id)
                if dataset is None:
                    raise ServiceError(404, "not_found", f"dataset {dataset_id} is not imported")
                if dataset.scoring_version != body.scoring_version:
                    raise ServiceError(
                        400,
                        "scoring_version",
                        f"dataset {dataset_id} was imported as {dataset.scoring_version}, "
                        f"not {body.scoring_version}",
                    )
            now = iso()
            exp = Experiment(
                id=str(uuid.uuid4()),
                name=body.name,
                status="RUNNING" if body.start else "DRAFT",
                search_profile=space.profile,
                search_space_json=_dumps(space.parameters),
                scoring_version=body.scoring_version,
                scorer_name=scorer_name_for(body.scoring_version),
                execution_mode=body.execution_mode,
                budget=body.trials,
                seed=body.seed,
                variability_penalty=body.variability_penalty,
                minos_commit=head_commit(),
                gatk_image=gatk_image(),
                gatk_image_id=image_id,
                happy_image=happy_image(),
                bcftools_image=bcftools_image(),
                training_dataset_ids=_dumps(list(body.training_dataset_ids)),
                held_out_dataset_ids=_dumps(list(body.held_out_dataset_ids)),
                top_n=body.top_n_validate,
                max_parallel_global=body.max_parallel_global,
                job_timeout_seconds=body.job_timeout_seconds,
                max_attempts=body.max_attempts,
                gatk_threads=body.gatk_threads,
                memory_gb=body.memory_gb,
                min_free_disk_mb=body.min_free_disk_mb,
                baseline_enqueued=False,
                resume_status=None,
                selection_json=None,
                created_at=now,
                updated_at=now,
            )
            db.add(exp)
            db.flush()
            if body.start:
                self._pump(db, exp)
            _audit(
                db,
                actor=actor,
                action="experiment.create",
                target_type="experiment",
                target_id=exp.id,
                result="ok",
                detail={"name": exp.name, "start": body.start, "execution_mode": body.execution_mode},
                idempotency_key=idempotency_key,
            )
            return self._experiment_view(db, exp)

    def list_experiments(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = list(db.scalars(select(Experiment).order_by(Experiment.created_at)))
            return [self._experiment_view(db, row) for row in rows]

    def get_experiment(self, key: str) -> dict[str, Any]:
        with self._db() as db:
            return self._experiment_view(db, self._by_key(db, key))

    def pause(self, key: str, *, actor: str = "system", idempotency_key: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            exp = self._by_key(db, key)
            if _replay(db, idempotency_key, "experiment.pause", exp.id) is not None:
                return self._experiment_view(db, exp)
            if exp.status == "PAUSED":
                return self._experiment_view(db, exp)
            if exp.status not in {"RUNNING", "VALIDATING"}:
                raise ServiceError(409, "status", f"cannot pause status {exp.status}")
            exp.resume_status = exp.status
            exp.status = "PAUSED"
            exp.updated_at = iso()
            self._expire(db, utc_now())
            _audit(
                db,
                actor=actor,
                action="experiment.pause",
                target_type="experiment",
                target_id=exp.id,
                result="ok",
                detail={"status": exp.status},
                idempotency_key=idempotency_key,
            )
            return self._experiment_view(db, exp)

    def resume(self, key: str, *, actor: str = "system", idempotency_key: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            exp = self._by_key(db, key)
            if _replay(db, idempotency_key, "experiment.resume", exp.id) is not None:
                return self._experiment_view(db, exp)
            if exp.status != "PAUSED":
                raise ServiceError(409, "status", f"cannot resume status {exp.status}")
            exp.status = exp.resume_status or "RUNNING"
            exp.resume_status = None
            exp.updated_at = iso()
            self._pump(db, exp)
            _audit(
                db,
                actor=actor,
                action="experiment.resume",
                target_type="experiment",
                target_id=exp.id,
                result="ok",
                detail={"status": exp.status},
                idempotency_key=idempotency_key,
            )
            return self._experiment_view(db, exp)

    def start(self, key: str, *, actor: str = "system", idempotency_key: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            exp = self._by_key(db, key)
            if _replay(db, idempotency_key, "experiment.start", exp.id) is not None:
                return self._experiment_view(db, exp)
            if exp.status != "DRAFT":
                raise ServiceError(409, "status", f"cannot start status {exp.status}")
            exp.status = "RUNNING"
            exp.updated_at = iso()
            self._pump(db, exp)
            _audit(
                db,
                actor=actor,
                action="experiment.start",
                target_type="experiment",
                target_id=exp.id,
                result="ok",
                detail={"status": exp.status},
                idempotency_key=idempotency_key,
            )
            return self._experiment_view(db, exp)

    def stop(self, key: str, *, actor: str = "system", idempotency_key: str | None = None) -> dict[str, Any]:
        """Stop new work. Leased GATK jobs are left to finish."""
        with self._db() as db:
            exp = self._by_key(db, key)
            if _replay(db, idempotency_key, "experiment.stop", exp.id) is not None:
                return self._experiment_view(db, exp)
            if exp.status in {"STOPPING", "STOPPED", "CANCELLED"}:
                return self._experiment_view(db, exp)
            if exp.status in {"COMPLETED", "FAILED"}:
                raise ServiceError(409, "status", f"cannot stop status {exp.status}")
            if exp.status == "DRAFT":
                exp.status = "CANCELLED"
                exp.updated_at = iso()
            else:
                exp.status = "STOPPING"
                exp.updated_at = iso()
                self._finish_stopping(db, exp)
            _audit(
                db,
                actor=actor,
                action="experiment.stop",
                target_type="experiment",
                target_id=exp.id,
                result="ok",
                detail={"status": exp.status},
                idempotency_key=idempotency_key,
            )
            return self._experiment_view(db, exp)

    def cancel(self, key: str, *, actor: str = "system", idempotency_key: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            exp = self._by_key(db, key)
            if _replay(db, idempotency_key, "experiment.cancel", exp.id) is not None:
                return self._experiment_view(db, exp)
            if exp.status in {"COMPLETED", "CANCELLED"}:
                raise ServiceError(409, "status", f"cannot cancel status {exp.status}")
            exp.status = "CANCELLED"
            exp.updated_at = iso()
            for job in self._jobs(db, exp.id):
                if job.state in ACTIVE:
                    job.state = "CANCELLED"
                    job.lease_token = None
                    job.updated_at = iso()
                    job.error_class = job.error_class or "cancelled"
                    _note_job(db, job, "CANCELLED", "experiment cancelled")
            self._finalize_open_trials(db, exp)
            _audit(
                db,
                actor=actor,
                action="experiment.cancel",
                target_type="experiment",
                target_id=exp.id,
                result="ok",
                detail={"status": exp.status},
                idempotency_key=idempotency_key,
            )
            return self._experiment_view(db, exp)

    def leaderboard(self, key: str) -> dict[str, Any]:
        with self._db() as db:
            exp = self._by_key(db, key)
            trials = self._trials(db, exp.id)
            ranked = [trial for trial in trials if trial.objective is not None and trial.told]
            ranked.sort(key=lambda trial: (-float(trial.objective or 0), trial.config_sha256))
            return {
                "experiment_id": exp.id,
                "name": exp.name,
                "scoring_version": exp.scoring_version,
                "scorer_name": exp.scorer_name,
                "execution_mode": exp.execution_mode,
                "score_source": exp.execution_mode,
                "rows": [
                    {
                        "trial_id": trial.id,
                        "optuna_number": trial.optuna_number,
                        "objective": trial.objective,
                        "state": trial.state,
                        "is_baseline": trial.is_baseline,
                        "config_sha256": trial.config_sha256,
                        "score_source": trial.score_source,
                        "held_out_mean": _held_out_mean(self._jobs(db, exp.id), trial.id, exp.execution_mode),
                    }
                    for trial in ranked
                ],
            }

    def trial_rows(self, key: str) -> dict[str, Any]:
        with self._db() as db:
            exp = self._by_key(db, key)
            jobs = self._jobs(db, exp.id)
            rows = []
            for trial in self._trials(db, exp.id):
                related = [job for job in jobs if job.trial_id == trial.id]
                rows.append(
                    {
                        "trial_id": trial.id,
                        "optuna_number": trial.optuna_number,
                        "state": trial.state,
                        "told": trial.told,
                        "is_baseline": trial.is_baseline,
                        "objective": trial.objective,
                        "score_source": trial.score_source,
                        "config_sha256": trial.config_sha256,
                        "params": _loads(trial.params_json, {}),
                        "failure_reason": trial.failure_reason,
                        "jobs": [
                            {
                                "job_id": job.id,
                                "dataset_id": job.dataset_id,
                                "role": job.role,
                                "state": job.state,
                                "attempt": job.attempt,
                                "official_score": job.official_score,
                                "mock_objective": job.mock_objective,
                                "score_source": job.score_source,
                                "error_class": job.error_class,
                                "worker_id": job.worker_id,
                            }
                            for job in related
                        ],
                    }
                )
            return {"experiment_id": exp.id, "trials": rows}

    def export_conf(self, key: str) -> str:
        with self._db() as db:
            exp = self._by_key(db, key)
            if exp.status == "STOPPED":
                raise ServiceError(
                    409,
                    "not_validated",
                    "Held-out verification did not finish. Export is blocked.",
                )
            if exp.status != "COMPLETED" or not exp.selection_json:
                raise ServiceError(409, "not_ready", "export is available after the study completes")
            selection = _loads(exp.selection_json, {})
            config = selection.get("config")
            if not isinstance(config, dict):
                raise ServiceError(409, "not_ready", "selection has no config")
            header = [
                f"experiment={exp.name}",
                f"experiment_id={exp.id}",
                f"minos_commit={exp.minos_commit}",
                f"scoring_version={exp.scoring_version}",
                f"scorer_name={exp.scorer_name}",
                f"score_source={selection.get('score_source')}",
                f"gatk_image={exp.gatk_image}",
                f"gatk_image_id={exp.gatk_image_id}",
                f"happy_image={exp.happy_image}",
                f"bcftools_image={exp.bcftools_image}",
                f"config_sha256={selection.get('winner_config_sha256')}",
                "Do not submit this file automatically. Copy it only after you review the provenance.",
                SELECTION_RULE,
            ]
            return dump_conf(config, header)

    def dashboard(self) -> dict[str, Any]:
        with self._db() as db:
            workers = [_worker_view(row) for row in db.scalars(select(Worker).order_by(Worker.id))]
            experiments = [
                self._experiment_view(db, row)
                for row in db.scalars(select(Experiment).order_by(Experiment.created_at.desc()))
            ]
            failures = []
            for job in db.scalars(
                select(Job)
                .where(Job.error_class.is_not(None))
                .order_by(Job.updated_at.desc())
                .limit(20)
            ):
                failures.append(
                    {
                        "job_id": job.id,
                        "experiment_id": job.experiment_id,
                        "dataset_id": job.dataset_id,
                        "state": job.state,
                        "error_class": job.error_class,
                        "message": (job.error_message or "")[:180],
                    }
                )
            return {"workers": workers, "experiments": experiments, "failures": failures}

    def set_scheduling(
        self,
        worker_id: str,
        mode: str,
        *,
        actor: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if mode not in SCHEDULING_MODES:
            raise ServiceError(400, "scheduling_mode", "mode must be enabled, paused, or draining")
        action = f"worker.{mode}"
        with self._db() as db:
            if _replay(db, idempotency_key, action, worker_id) is not None:
                worker = db.get(Worker, worker_id)
                if worker is None:
                    raise ServiceError(404, "not_found", "worker was not found")
                return _worker_view(worker)
            worker = db.get(Worker, worker_id)
            if worker is None:
                raise ServiceError(404, "not_found", "worker was not found")
            changed = worker.scheduling_mode != mode
            worker.scheduling_mode = mode
            worker.updated_at = iso()
            if changed:
                _audit(
                    db,
                    actor=actor,
                    action=action,
                    target_type="worker",
                    target_id=worker.id,
                    result="ok",
                    detail={"scheduling_mode": mode},
                    idempotency_key=idempotency_key,
                )
            return _worker_view(worker)

    def set_policy(
        self,
        worker_id: str,
        *,
        max_concurrent_jobs: int | None,
        reset: bool,
        actor: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if reset == (max_concurrent_jobs is not None):
            raise ServiceError(400, "policy", "send max_concurrent_jobs or reset, not both")
        with self._db() as db:
            if _replay(db, idempotency_key, "worker.policy", worker_id) is not None:
                worker = db.get(Worker, worker_id)
                if worker is None:
                    raise ServiceError(404, "not_found", "worker was not found")
                return _worker_view(worker)
            worker = db.get(Worker, worker_id)
            if worker is None:
                raise ServiceError(404, "not_found", "worker was not found")
            if reset:
                worker.policy_max_parallel = None
            else:
                assert max_concurrent_jobs is not None
                ceiling = min(int(worker.max_parallel_jobs), int(worker.cpu_count))
                if max_concurrent_jobs > ceiling:
                    raise ServiceError(
                        400,
                        "policy_limit",
                        f"max_concurrent_jobs cannot exceed the worker capability {ceiling}",
                    )
                worker.policy_max_parallel = max_concurrent_jobs
            worker.updated_at = iso()
            _audit(
                db,
                actor=actor,
                action="worker.policy",
                target_type="worker",
                target_id=worker.id,
                result="ok",
                detail={"policy_max_parallel": worker.policy_max_parallel},
                idempotency_key=idempotency_key,
            )
            return _worker_view(worker)

    def retry_job(self, job_id: str, *, actor: str, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            if _replay(db, idempotency_key, "job.retry", job_id) is not None:
                job = self._require_job(db, job_id)
                return {"job_id": job.id, "state": job.state, "attempt": job.attempt}
            job = self._require_job(db, job_id)
            trial = db.get(Trial, job.trial_id)
            exp = self._require_experiment(db, job.experiment_id)
            if trial is None:
                raise ServiceError(404, "not_found", "trial was not found")
            if trial.told:
                raise ServiceError(409, "finalized", "the Optuna trial was already finalized")
            if exp.status not in {"RUNNING", "VALIDATING", "PAUSED"}:
                raise ServiceError(409, "status", f"cannot retry while the experiment is {exp.status}")
            if job.state not in {"FAILED", "TIMED_OUT"}:
                raise ServiceError(409, "status", f"cannot retry a job in state {job.state}")
            if job.error_class in NON_RETRYABLE:
                raise ServiceError(409, "not_retryable", f"{job.error_class} is not retryable")
            if job.attempt >= job.max_attempts:
                raise ServiceError(409, "attempts", "attempt limit reached")
            job.attempt += 1
            job.state = "PENDING"
            job.lease_token = None
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = iso()
            _note_job(db, job, "PENDING", "admin retry")
            _audit(
                db,
                actor=actor,
                action="job.retry",
                target_type="job",
                target_id=job.id,
                result="ok",
                detail={"attempt": job.attempt},
                idempotency_key=idempotency_key,
            )
            if exp.status != "PAUSED":
                self._pump(db, exp)
            return {"job_id": job.id, "state": job.state, "attempt": job.attempt}

    def create_export(self, key: str, *, actor: str, idempotency_key: str | None = None) -> dict[str, Any]:
        if idempotency_key:
            with self._db() as db:
                prior = _replay(db, idempotency_key, "experiment.export")
                if prior is not None:
                    row = db.get(ExportRecord, prior.target_id)
                    if row is not None:
                        return _export_view(row)
        text = self.export_conf(key)
        with self._db() as db:
            exp = self._by_key(db, key)
            selection = _loads(exp.selection_json, {})
            holdouts = _loads(exp.held_out_dataset_ids, [])
            research_only = not holdouts or selection.get("held_out_mean") is None
            warning = ""
            if research_only:
                warning = (
                    "This file is the training-objective winner. "
                    "The study had no completed held-out verification."
                )
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            provenance = {
                "experiment_id": exp.id,
                "experiment_name": exp.name,
                "minos_commit": exp.minos_commit,
                "scoring_version": exp.scoring_version,
                "scorer_name": exp.scorer_name,
                "execution_mode": exp.execution_mode,
                "gatk_image": exp.gatk_image,
                "gatk_image_id": exp.gatk_image_id,
                "happy_image": exp.happy_image,
                "bcftools_image": exp.bcftools_image,
                "export_text_sha256": digest,
                "selection": selection,
                "research_only": research_only,
                "warning": warning,
            }
            row = ExportRecord(
                id=str(uuid.uuid4()),
                experiment_id=exp.id,
                actor=actor[:128],
                config_text=text,
                provenance_json=_dumps(provenance),
                content_sha256=digest,
                research_only=research_only,
                warning=warning,
                created_at=iso(),
            )
            db.add(row)
            _audit(
                db,
                actor=actor,
                action="experiment.export",
                target_type="export",
                target_id=row.id,
                result="ok",
                detail={"experiment_id": exp.id, "research_only": research_only},
                idempotency_key=idempotency_key,
            )
            return _export_view(row)

    def export_record(self, export_id: str, part: str) -> tuple[str, str, str]:
        if part not in {"conf", "provenance"}:
            raise ServiceError(400, "part", "part must be conf or provenance")
        with self._db() as db:
            row = db.get(ExportRecord, export_id)
            if row is None:
                raise ServiceError(404, "not_found", "export was not found")
            if part == "conf":
                return row.config_text, "text/plain; charset=utf-8", "gatk.conf"
            return row.provenance_json, "application/json", "gatk.provenance.json"

    def ready(self) -> bool:
        with self._db() as db:
            db.execute(select(func.count()).select_from(Experiment))
        return True

    # --- scheduling internals ---

    def _store_manifest(self, prepared: dict[str, Any]) -> dict[str, Any]:
        with self._db() as db:
            existing = db.get(Dataset, prepared["dataset_id"])
            if existing is not None:
                if existing.manifest_sha256 != prepared["manifest_sha256"]:
                    raise ServiceError(
                        409,
                        "manifest_changed",
                        "dataset id already exists with a different manifest hash",
                    )
                files = list(db.scalars(select(DatasetFile).where(DatasetFile.dataset_id == existing.id)))
                return _dataset_view(existing, file_count=len(files))
            now = iso()
            total = sum(int(item["size_bytes"]) for item in prepared["files"])
            dataset = Dataset(
                id=prepared["dataset_id"],
                name=str(prepared.get("name") or prepared["dataset_id"]),
                region=prepared["region"],
                chromosome=prepared.get("chromosome"),
                reference_build=prepared["reference_build"],
                reference_header=prepared["reference_header"][:256],
                manifest_sha256=prepared["manifest_sha256"],
                reference_sha256=prepared["reference_sha256"],
                scoring_version=prepared["scoring_version"],
                source=str(prepared.get("source") or ""),
                license_note=str(prepared.get("license_note") or ""),
                total_bytes=total,
                created_at=now,
            )
            db.add(dataset)
            for item in prepared["files"]:
                db.add(
                    DatasetFile(
                        id=str(uuid.uuid4()),
                        dataset_id=dataset.id,
                        role=item["role"],
                        relative_path=item["relative_path"],
                        sha256=item["sha256"],
                        size_bytes=int(item["size_bytes"]),
                        local_path=item["path"],
                    )
                )
            return _dataset_view(dataset, file_count=len(prepared["files"]))

    def _resolve_space(self, body: ExperimentCreate) -> SearchSpace:
        if body.search_space is None:
            return load_search_space(default_search_path(), body.search_profile)
        document = body.search_space
        if body.search_profile in document:
            return space_from_document(document, body.search_profile)
        if "parameters" in document:
            return parse_search_parameters(document["parameters"], profile=body.search_profile)
        raise SearchSpaceError(f"Search profile {body.search_profile!r} was not provided")

    def _pump(self, db: Session, exp: Experiment) -> None:
        self._expire(db, utc_now())
        if exp.status in {"RUNNING", "VALIDATING"}:
            space = self._space(exp)
            study = self._ensure_study(exp, space)
            self._adopt_orphans(db, exp, study, space)
        self._finalize_open_trials(db, exp)
        if exp.status == "RUNNING":
            space = self._space(exp)
            study = self._ensure_study(exp, space)
            while self._can_ask(db, exp):
                self._ask_one(db, exp, study, space)
                self._finalize_open_trials(db, exp)
            self._maybe_validate(db, exp)
            self._finalize_open_trials(db, exp)
            self._maybe_select(db, exp)
        elif exp.status == "STOPPING":
            self._finish_stopping(db, exp)
        elif exp.status == "VALIDATING":
            self._maybe_select(db, exp)

    def _finish_stopping(self, db: Session, exp: Experiment) -> None:
        if exp.status != "STOPPING":
            return
        now = iso()
        for job in self._jobs(db, exp.id):
            if job.state != "PENDING":
                continue
            job.state = "CANCELLED"
            job.error_class = "stopped"
            job.error_message = "experiment stopped before the job was leased"
            job.lease_token = None
            job.worker_id = None
            job.updated_at = now
            _note_job(db, job, "CANCELLED", "stopped before lease")
        jobs = self._jobs(db, exp.id)
        if any(job.state in {"LEASED", "RUNNING"} for job in jobs):
            return
        self._finalize_open_trials(db, exp)
        if exp.status != "STOPPING":
            return
        holdouts = _loads(exp.held_out_dataset_ids, [])
        trials = self._trials(db, exp.id)
        if not holdouts:
            self._select_training_only(db, exp, trials)
            return
        best = None
        for trial in trials:
            if trial.objective is None:
                continue
            if best is None or float(trial.objective) > float(best.objective or 0):
                best = trial
        payload: dict[str, Any] = {
            "rule": SELECTION_RULE,
            "export_allowed": False,
            "reason": "stopped before held-out verification completed",
            "score_source": exp.execution_mode,
            "scoring_version": exp.scoring_version,
            "scorer_name": exp.scorer_name,
        }
        if best is not None:
            payload["best_training_objective"] = best.objective
            payload["best_training_trial_id"] = best.id
            payload["best_training_config_sha256"] = best.config_sha256
        exp.selection_json = _dumps(payload)
        exp.status = "STOPPED"
        exp.updated_at = iso()

    def _space(self, exp: Experiment) -> SearchSpace:
        return parse_search_parameters(_loads(exp.search_space_json, {}), profile=exp.search_profile)

    def _ensure_study(self, exp: Experiment, space: SearchSpace) -> optuna.Study:
        study = optuna.create_study(
            study_name=exp.id,
            storage=self.storage(),
            sampler=TPESampler(seed=exp.seed, multivariate=True),
            direction="maximize",
            load_if_exists=True,
        )
        if not exp.baseline_enqueued:
            study.enqueue_trial(
                baseline_params(space),
                user_attrs={"baseline": True},
                skip_if_exists=True,
            )
            exp.baseline_enqueued = True
        return study

    def _can_ask(self, db: Session, exp: Experiment) -> bool:
        training = _loads(exp.training_dataset_ids, [])
        asked = db.scalar(select(func.count()).select_from(Trial).where(Trial.experiment_id == exp.id)) or 0
        if int(asked) >= exp.budget:
            return False
        occupied = self._occupied(db, exp.id)
        needed = len(training)
        if occupied > 0 and occupied + needed > exp.max_parallel_global:
            return False
        return needed > 0

    def _occupied(self, db: Session, experiment_id: str) -> int:
        count = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.experiment_id == experiment_id, Job.state.in_(ACTIVE))
        )
        return int(count or 0)

    def _ask_one(self, db: Session, exp: Experiment, study: optuna.Study, space: SearchSpace) -> None:
        opt_trial = study.ask()
        try:
            params = suggest(opt_trial, space)
            config = full_config(params)
        except Exception as exc:
            log.warning("trial %s failed before a job was created: %s", opt_trial.number, exc)
            study.tell(opt_trial.number, state=TrialState.FAIL, skip_if_finished=True)
            self._insert_trial(
                db,
                exp,
                number=opt_trial.number,
                params={},
                config={},
                state="FAILED",
                told=True,
                is_baseline=False,
                failure=str(exc),
            )
            return
        digest = config_sha256(config)
        trial = self._insert_trial(
            db,
            exp,
            number=opt_trial.number,
            params=params,
            config=config,
            state="RUNNING",
            told=False,
            is_baseline=bool(opt_trial.user_attrs.get("baseline")),
            failure=None,
        )
        for dataset_id in _loads(exp.training_dataset_ids, []):
            self._add_job(db, exp, trial, dataset_id, role="training", counts=True, config=config, digest=digest)

    def _insert_trial(
        self,
        db: Session,
        exp: Experiment,
        *,
        number: int,
        params: dict[str, Any],
        config: dict[str, Any],
        state: str,
        told: bool,
        is_baseline: bool,
        failure: str | None,
    ) -> Trial:
        now = iso()
        trial = Trial(
            id=str(uuid.uuid4()),
            experiment_id=exp.id,
            optuna_number=number,
            state=state,
            params_json=_dumps(params),
            full_config_json=_dumps(config),
            config_sha256=config_sha256(config) if config else "0" * 64,
            is_baseline=is_baseline,
            objective=None,
            score_source=None,
            told=told,
            failure_reason=failure,
            created_at=now,
            updated_at=now,
        )
        db.add(trial)
        db.flush()
        return trial

    def _add_job(
        self,
        db: Session,
        exp: Experiment,
        trial: Trial,
        dataset_id: str,
        *,
        role: str,
        counts: bool,
        config: dict[str, Any],
        digest: str,
    ) -> Job:
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise ServiceError(404, "not_found", f"dataset {dataset_id} disappeared")
        key = evaluation_cache_key(
            config_sha256=digest,
            manifest_sha256=dataset.manifest_sha256,
            reference_sha256=dataset.reference_sha256,
            minos_commit=exp.minos_commit,
            scoring_version=exp.scoring_version,
            gatk_image=exp.gatk_image,
            gatk_image_id=exp.gatk_image_id,
            happy_image=exp.happy_image,
            bcftools_image=exp.bcftools_image,
            threads=exp.gatk_threads,
            memory_gb=exp.memory_gb,
            execution_mode=exp.execution_mode,
        )
        cached = db.get(EvalCache, key)
        now = iso()
        job = Job(
            id=str(uuid.uuid4()),
            experiment_id=exp.id,
            trial_id=trial.id,
            dataset_id=dataset_id,
            role=role,
            counts_toward_objective=counts,
            state="PENDING",
            config_json=_dumps(config),
            config_sha256=digest,
            cache_key=key,
            lease_token=None,
            lease_expires_at=None,
            worker_id=None,
            attempt=1,
            max_attempts=exp.max_attempts,
            timeout_seconds=exp.job_timeout_seconds,
            threads=exp.gatk_threads,
            memory_gb=exp.memory_gb,
            execution_mode=exp.execution_mode,
            official_score=None,
            mock_objective=None,
            score_source=None,
            metrics_json=None,
            error_class=None,
            error_message=None,
            elapsed_seconds=None,
            provenance_json=None,
            created_at=now,
            updated_at=now,
        )
        if cached is not None and cached.score_source == exp.execution_mode:
            job.state = "COMPLETED"
            job.official_score = cached.official_score
            job.mock_objective = cached.mock_objective
            job.score_source = cached.score_source
            job.metrics_json = cached.metrics_json
            provenance = _loads(cached.provenance_json, {})
            provenance["cache_hit"] = True
            job.provenance_json = _dumps(provenance)
            job.elapsed_seconds = 0
        db.add(job)
        db.flush()
        if job.state == "COMPLETED":
            _note_job(db, job, "COMPLETED", "evaluation cache")
        else:
            _note_job(db, job, "PENDING", "created")
        return job

    def _adopt_orphans(self, db: Session, exp: Experiment, study: optuna.Study, space: SearchSpace) -> None:
        del space
        known = {trial.optuna_number: trial for trial in self._trials(db, exp.id)}
        for frozen in study.trials:
            if frozen.state == TrialState.WAITING:
                # An enqueued baseline (or other waiting suggestion) has no
                # parameters yet. ask() consumes it. It is not a crashed trial.
                continue
            row = known.get(frozen.number)
            if row is not None:
                if frozen.state.is_finished() and not row.told:
                    row.told = True
                    row.updated_at = iso()
                    if frozen.value is not None and row.objective is None:
                        row.objective = float(frozen.value)
                    if row.state in {"RUNNING", "ASKED"}:
                        row.state = "COMPLETED" if frozen.state == TrialState.COMPLETE else "FAILED"
                continue
            if frozen.state.is_finished():
                self._insert_trial(
                    db,
                    exp,
                    number=frozen.number,
                    params=dict(frozen.params),
                    config={},
                    state="COMPLETED" if frozen.state == TrialState.COMPLETE else "FAILED",
                    told=True,
                    is_baseline=bool(frozen.user_attrs.get("baseline")),
                    failure=None if frozen.state == TrialState.COMPLETE else "recovered finished orphan",
                )
                continue
            if not frozen.params:
                study.tell(frozen.number, state=TrialState.FAIL, skip_if_finished=True)
                self._insert_trial(
                    db,
                    exp,
                    number=frozen.number,
                    params={},
                    config={},
                    state="FAILED",
                    told=True,
                    is_baseline=False,
                    failure="asked trial had no parameters after a crash",
                )
                continue
            try:
                params = dict(frozen.params)
                config = full_config(params)
            except SearchSpaceError as exc:
                study.tell(frozen.number, state=TrialState.FAIL, skip_if_finished=True)
                self._insert_trial(
                    db,
                    exp,
                    number=frozen.number,
                    params=dict(frozen.params),
                    config={},
                    state="FAILED",
                    told=True,
                    is_baseline=False,
                    failure=str(exc),
                )
                continue
            digest = config_sha256(config)
            trial = self._insert_trial(
                db,
                exp,
                number=frozen.number,
                params=params,
                config=config,
                state="RUNNING",
                told=False,
                is_baseline=bool(frozen.user_attrs.get("baseline")),
                failure=None,
            )
            for dataset_id in _loads(exp.training_dataset_ids, []):
                self._add_job(
                    db,
                    exp,
                    trial,
                    dataset_id,
                    role="training",
                    counts=True,
                    config=config,
                    digest=digest,
                )

    def _finalize_open_trials(self, db: Session, exp: Experiment) -> None:
        if exp.status not in {"RUNNING", "VALIDATING", "PAUSED", "CANCELLED", "STOPPING"}:
            return
        try:
            study = optuna.load_study(study_name=exp.id, storage=self.storage())
        except KeyError:
            return
        for trial in self._trials(db, exp.id):
            if trial.counts_training_pending(self._jobs(db, exp.id)):
                continue
            self._finalize_trial(db, exp, trial, study)

    def _finalize_touched(self, db: Session) -> None:
        experiment_ids = {
            job.experiment_id
            for job in db.scalars(select(Job).where(Job.state.in_(TERMINAL_JOB)))
        }
        for experiment_id in experiment_ids:
            exp = db.get(Experiment, experiment_id)
            if exp is not None and exp.status not in {"COMPLETED", "FAILED"}:
                self._pump(db, exp)

    def _finalize_trial(self, db: Session, exp: Experiment, trial: Trial, study: optuna.Study) -> None:
        if trial.told:
            return
        jobs = [
            job
            for job in self._jobs(db, exp.id)
            if job.trial_id == trial.id and job.counts_toward_objective
        ]
        if not jobs:
            return
        if any(job.state in ACTIVE for job in jobs):
            return
        frozen = next((item for item in study.trials if item.number == trial.optuna_number), None)
        if frozen is not None and frozen.state.is_finished():
            trial.told = True
            trial.updated_at = iso()
            if frozen.value is not None and trial.objective is None:
                trial.objective = float(frozen.value)
            if trial.state in {"RUNNING", "ASKED"}:
                trial.state = "COMPLETED" if frozen.state == TrialState.COMPLETE else "FAILED"
            return
        if exp.status == "CANCELLED" or any(job.state == "CANCELLED" for job in jobs):
            study.tell(trial.optuna_number, state=TrialState.PRUNED, skip_if_finished=True)
            trial.told = True
            trial.state = "CANCELLED"
            trial.failure_reason = "cancelled"
            trial.updated_at = iso()
            return
        if all(job.state == "COMPLETED" for job in jobs):
            scores: list[float] = []
            for job in jobs:
                value = job.official_score if exp.execution_mode == "official" else job.mock_objective
                if value is None:
                    study.tell(trial.optuna_number, state=TrialState.FAIL, skip_if_finished=True)
                    trial.told = True
                    trial.state = "FAILED"
                    trial.failure_reason = "completed job is missing a score"
                    trial.updated_at = iso()
                    return
                scores.append(float(value))
            value = objective_value(scores, exp.variability_penalty)
            study.tell(trial.optuna_number, values=value, skip_if_finished=True)
            trial.told = True
            trial.state = "COMPLETED"
            trial.objective = value
            trial.score_source = exp.execution_mode
            trial.updated_at = iso()
            return
        study.tell(trial.optuna_number, state=TrialState.FAIL, skip_if_finished=True)
        trial.told = True
        trial.state = "FAILED"
        trial.failure_reason = ",".join(
            job.error_class or job.state for job in jobs if job.state != "COMPLETED"
        )[:1000]
        trial.updated_at = iso()

    def _maybe_validate(self, db: Session, exp: Experiment) -> None:
        if exp.status != "RUNNING":
            return
        trials = self._trials(db, exp.id)
        if len(trials) < exp.budget or any(not trial.told for trial in trials):
            return
        holdouts = _loads(exp.held_out_dataset_ids, [])
        if not holdouts:
            self._select_training_only(db, exp, trials)
            return
        exp.status = "VALIDATING"
        exp.updated_at = iso()
        for trial in _validation_set(trials, exp.top_n):
            config = _loads(trial.full_config_json, {})
            for dataset_id in holdouts:
                exists = any(
                    job.trial_id == trial.id and job.dataset_id == dataset_id and job.role == "held_out"
                    for job in self._jobs(db, exp.id)
                )
                if exists:
                    continue
                self._add_job(
                    db,
                    exp,
                    trial,
                    dataset_id,
                    role="held_out",
                    counts=False,
                    config=config,
                    digest=trial.config_sha256,
                )

    def _maybe_select(self, db: Session, exp: Experiment) -> None:
        if exp.status != "VALIDATING":
            return
        trials = self._trials(db, exp.id)
        picked = _validation_set(trials, exp.top_n)
        jobs = self._jobs(db, exp.id)
        relevant = [
            job
            for job in jobs
            if job.role == "held_out" and any(job.trial_id == trial.id for trial in picked)
        ]
        if any(job.state in ACTIVE for job in relevant):
            return
        eligible: list[tuple[float, float, str, Trial]] = []
        for trial in picked:
            owned = [job for job in relevant if job.trial_id == trial.id]
            if not owned or any(job.state != "COMPLETED" for job in owned):
                continue
            scores = []
            for job in owned:
                value = job.official_score if exp.execution_mode == "official" else job.mock_objective
                if value is None:
                    scores = None
                    break
                scores.append(float(value))
            if not scores:
                continue
            eligible.append((sum(scores) / len(scores), float(trial.objective or float("-inf")), trial.config_sha256, trial))
        if not eligible:
            exp.status = "FAILED"
            exp.updated_at = iso()
            exp.selection_json = _dumps({"rule": SELECTION_RULE, "error": "no held-out score was recorded"})
            return
        eligible.sort(key=lambda row: (-row[0], -row[1], row[2]))
        self._write_selection(db, exp, trials, eligible[0][3], held_out_mean=eligible[0][0])

    def _select_training_only(self, db: Session, exp: Experiment, trials: list[Trial]) -> None:
        completed = [trial for trial in trials if trial.state == "COMPLETED" and trial.objective is not None]
        if not completed:
            exp.status = "FAILED"
            exp.updated_at = iso()
            exp.selection_json = _dumps({"rule": SELECTION_RULE, "error": "no training score was recorded"})
            return
        completed.sort(key=lambda trial: (-float(trial.objective or 0), trial.config_sha256))
        self._write_selection(db, exp, trials, completed[0], held_out_mean=None)

    def _write_selection(
        self,
        db: Session,
        exp: Experiment,
        trials: list[Trial],
        winner: Trial,
        *,
        held_out_mean: float | None,
    ) -> None:
        baseline = next((trial for trial in trials if trial.is_baseline), None)
        jobs = self._jobs(db, exp.id)
        baseline_held = None if baseline is None else _held_out_mean(jobs, baseline.id, exp.execution_mode)
        config = _loads(winner.full_config_json, {})
        selection: dict[str, Any] = {
            "rule": SELECTION_RULE,
            "winner_trial_id": winner.id,
            "winner_optuna_number": winner.optuna_number,
            "winner_config_sha256": winner.config_sha256,
            "config": config,
            "training_objective": winner.objective,
            "held_out_mean": held_out_mean,
            "baseline_trial_id": None if baseline is None else baseline.id,
            "baseline_training_objective": None if baseline is None else baseline.objective,
            "baseline_held_out_mean": baseline_held,
            "score_source": winner.score_source or exp.execution_mode,
            "scoring_version": exp.scoring_version,
            "scorer_name": exp.scorer_name,
            "minos_commit": exp.minos_commit,
            "gatk_image": exp.gatk_image,
            "gatk_image_id": exp.gatk_image_id,
        }
        if held_out_mean is not None and baseline_held is not None:
            selection["held_out_delta_vs_baseline"] = held_out_mean - baseline_held
        elif winner.objective is not None and baseline is not None and baseline.objective is not None:
            selection["training_delta_vs_baseline"] = float(winner.objective) - float(baseline.objective)
        exp.selection_json = _dumps(selection)
        exp.status = "COMPLETED"
        exp.updated_at = iso()

    def _claim_once(self, db: Session, worker: Worker, body: ClaimRequest) -> dict[str, Any] | None:
        if body.execution_mode != worker.execution_mode:
            raise ServiceError(400, "execution_mode", "claim execution_mode does not match the worker")
        worker.cached_datasets = _dumps(body.cached_dataset_ids)
        worker.free_disk_mb = body.free_disk_mb
        worker.updated_at = iso()
        running = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.worker_id == worker.id, Job.state.in_(("LEASED", "RUNNING")))
        )
        if worker.scheduling_mode in {"paused", "draining"}:
            return None
        cap = effective_parallel(worker)
        if int(running or 0) >= cap or body.running_jobs >= cap:
            return None
        cached = set(body.cached_dataset_ids)
        candidates = []
        for job in db.scalars(select(Job).where(Job.state == "PENDING").order_by(Job.created_at)):
            exp = db.get(Experiment, job.experiment_id)
            if exp is None or not _assignable(worker, exp, body):
                continue
            leases = db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.experiment_id == exp.id, Job.state.in_(("LEASED", "RUNNING")))
            )
            if int(leases or 0) >= exp.max_parallel_global:
                continue
            dataset = db.get(Dataset, job.dataset_id)
            if dataset is None:
                continue
            needed_mb = 0 if job.dataset_id in cached else int(dataset.total_bytes / (1024 * 1024)) + 1
            if body.free_disk_mb < exp.min_free_disk_mb + needed_mb:
                continue
            candidates.append((0 if job.dataset_id in cached else 1, job.created_at, job))
        candidates.sort(key=lambda item: (item[0], item[1]))
        now = utc_now()
        for _, _, job in candidates:
            exp = self._require_experiment(db, job.experiment_id)
            dataset = self._require_dataset(db, job.dataset_id)
            if self._fit_job_to_worker(db, job, exp, dataset, worker):
                continue
            token = secrets.token_hex(32)
            job.state = "LEASED"
            job.lease_token = token
            job.worker_id = worker.id
            job.error_class = None
            job.error_message = None
            job.lease_expires_at = after(job.timeout_seconds + self.lease_grace_seconds, now)
            job.updated_at = iso(now)
            _note_job(
                db,
                job,
                "LEASED",
                f"leased to {worker.id} with {job.threads} threads and {job.memory_gb} GB",
            )
            db.flush()
            return _job_payload(job, exp, dataset, token)
        return None

    def _fit_job_to_worker(
        self,
        db: Session,
        job: Job,
        exp: Experiment,
        dataset: Dataset,
        worker: Worker,
    ) -> bool:
        """Give an official job this computer's free CPU and memory.

        Returns True when a previous run at that size already has a score, so
        the caller keeps looking for a job that still needs the computer.
        """
        if exp.execution_mode != "official":
            return False
        threads, memory_gb = usable_resources(
            cpu_count=worker.cpu_count,
            ram_mb=worker.ram_mb,
            ram_used_mb=worker.ram_used_mb,
        )
        job.threads = threads
        job.memory_gb = memory_gb
        job.cache_key = evaluation_cache_key(
            config_sha256=job.config_sha256,
            manifest_sha256=dataset.manifest_sha256,
            reference_sha256=dataset.reference_sha256,
            minos_commit=exp.minos_commit,
            scoring_version=exp.scoring_version,
            gatk_image=exp.gatk_image,
            gatk_image_id=exp.gatk_image_id,
            happy_image=exp.happy_image,
            bcftools_image=exp.bcftools_image,
            threads=threads,
            memory_gb=memory_gb,
            execution_mode=exp.execution_mode,
        )
        cached = db.get(EvalCache, job.cache_key)
        if cached is None or cached.score_source != exp.execution_mode:
            return False
        now = iso()
        job.state = "COMPLETED"
        job.official_score = cached.official_score
        job.mock_objective = cached.mock_objective
        job.score_source = cached.score_source
        job.metrics_json = cached.metrics_json
        provenance = _loads(cached.provenance_json, {})
        provenance["cache_hit"] = True
        job.provenance_json = _dumps(provenance)
        job.elapsed_seconds = 0
        job.updated_at = now
        _note_job(db, job, "COMPLETED", "evaluation cache for this computer's resources")
        self._pump(db, exp)
        return True

    def _expire(self, db: Session, now: Any) -> None:
        for job in list(db.scalars(select(Job).where(Job.state.in_(("LEASED", "RUNNING"))))):
            if not job.lease_expires_at or parse_iso(job.lease_expires_at) > now:
                continue
            exp = db.get(Experiment, job.experiment_id)
            job.updated_at = iso(now)
            job.lease_token = None
            job.worker_id = None
            job.lease_expires_at = None
            job.error_class = "lease_expired"
            if exp is not None and exp.status != "CANCELLED" and job.attempt < job.max_attempts:
                job.attempt += 1
                job.state = "PENDING"
                _note_job(db, job, "PENDING", "lease expired; requeued")
            else:
                job.state = "TIMED_OUT"
                job.error_message = "lease expired"
                _note_job(db, job, "TIMED_OUT", "lease expired")

    def _remember_cache(self, db: Session, job: Job, exp: Experiment) -> None:
        if job.score_source != exp.execution_mode:
            return
        if exp.execution_mode == "official" and job.official_score is None:
            return
        if exp.execution_mode == "mock" and job.mock_objective is None:
            return
        if db.get(EvalCache, job.cache_key) is not None:
            return
        dataset = db.get(Dataset, job.dataset_id)
        if dataset is None:
            return
        db.add(
            EvalCache(
                cache_key=job.cache_key,
                official_score=job.official_score,
                mock_objective=job.mock_objective,
                score_source=job.score_source or exp.execution_mode,
                metrics_json=job.metrics_json or "{}",
                provenance_json=job.provenance_json or "{}",
                config_sha256=job.config_sha256,
                dataset_manifest_sha256=dataset.manifest_sha256,
                created_at=iso(),
            )
        )

    def _require_job(self, db: Session, job_id: str) -> Job:
        job = db.get(Job, job_id)
        if job is None:
            raise ServiceError(404, "not_found", "job was not found")
        return job

    def _require_active_lease(self, job: Job, worker_id: str, lease_token: str) -> None:
        if job.worker_id != worker_id or job.lease_token != lease_token or job.state not in {"LEASED", "RUNNING"}:
            raise ServiceError(409, "stale_lease", "lease token does not match the active lease")

    def _check_score_body(self, exp: Experiment, body: CompleteRequest) -> None:
        if exp.execution_mode == "mock":
            if body.score_source != "mock" or body.mock_objective is None or body.official_score is not None:
                raise ServiceError(
                    400,
                    "score_source",
                    "mock completion requires mock_objective and a null official_score",
                )
            return
        if body.score_source != "official" or body.official_score is None or body.mock_objective is not None:
            raise ServiceError(
                400,
                "score_source",
                "official completion requires official_score and a null mock_objective",
            )

    def _jobs(self, db: Session, experiment_id: str) -> list[Job]:
        return list(db.scalars(select(Job).where(Job.experiment_id == experiment_id)))

    def _trials(self, db: Session, experiment_id: str) -> list[Trial]:
        return list(
            db.scalars(select(Trial).where(Trial.experiment_id == experiment_id).order_by(Trial.optuna_number))
        )

    def _by_key(self, db: Session, key: str) -> Experiment:
        exp = db.get(Experiment, key)
        if exp is None:
            exp = db.scalar(select(Experiment).where(Experiment.name == key))
        if exp is None:
            raise ServiceError(404, "not_found", "experiment was not found")
        return exp

    def _require_experiment(self, db: Session, experiment_id: str) -> Experiment:
        exp = db.get(Experiment, experiment_id)
        if exp is None:
            raise ServiceError(404, "not_found", "experiment was not found")
        return exp

    def _require_dataset(self, db: Session, dataset_id: str) -> Dataset:
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise ServiceError(404, "not_found", "dataset was not found")
        return dataset

    def _experiment_view(self, db: Session, exp: Experiment) -> dict[str, Any]:
        trials = self._trials(db, exp.id)
        jobs = self._jobs(db, exp.id)
        counts = Counter(job.state for job in jobs)
        completed = [trial for trial in trials if trial.objective is not None]
        best = max(completed, key=lambda trial: float(trial.objective or 0), default=None)
        trend = []
        best_so_far = None
        for trial in trials:
            if trial.objective is None:
                continue
            value = float(trial.objective)
            best_so_far = value if best_so_far is None else max(best_so_far, value)
            trend.append(best_so_far)
        return {
            "id": exp.id,
            "name": exp.name,
            "status": exp.status,
            "search_profile": exp.search_profile,
            "scoring_version": exp.scoring_version,
            "scorer_name": exp.scorer_name,
            "execution_mode": exp.execution_mode,
            "budget": exp.budget,
            "seed": exp.seed,
            "variability_penalty": exp.variability_penalty,
            "minos_commit": exp.minos_commit,
            "gatk_image": exp.gatk_image,
            "gatk_image_id": exp.gatk_image_id,
            "happy_image": exp.happy_image,
            "bcftools_image": exp.bcftools_image,
            "training_dataset_ids": _loads(exp.training_dataset_ids, []),
            "held_out_dataset_ids": _loads(exp.held_out_dataset_ids, []),
            "top_n": exp.top_n,
            "max_parallel_global": exp.max_parallel_global,
            "trials_total": len(trials),
            "trials_told": sum(1 for trial in trials if trial.told),
            "job_counts": dict(counts),
            "best_objective": None if best is None else best.objective,
            "best_score_source": None if best is None else best.score_source,
            "best_trend": trend,
            "selection": _loads(exp.selection_json, None),
            "created_at": exp.created_at,
            "updated_at": exp.updated_at,
        }


def _env_loopback_ok() -> bool:
    import os

    return os.environ.get("MINOS_ALLOW_LOOPBACK_ADMIN", "1") != "0"


def _preferred_study(db: Session, experiments: list[Experiment]) -> Experiment | None:
    if not experiments:
        return None

    def rank(exp: Experiment) -> tuple[bool, str]:
        pending = db.scalar(
            select(func.count()).select_from(Job).where(Job.experiment_id == exp.id, Job.state == "PENDING")
        )
        return (int(pending or 0) > 0, exp.updated_at)

    return max(experiments, key=rank)


def _assignable(worker: Worker, exp: Experiment, body: ClaimRequest) -> bool:
    if exp.status not in {"RUNNING", "VALIDATING"}:
        return False
    if exp.execution_mode != worker.execution_mode or body.execution_mode != exp.execution_mode:
        return False
    if worker.scorer_version != exp.scoring_version or worker.scorer_name != exp.scorer_name:
        return False
    if worker.minos_commit != exp.minos_commit:
        return False
    if exp.gatk_threads > worker.cpu_count:
        return False
    if exp.memory_gb * 1024 > worker.ram_mb:
        return False
    if exp.execution_mode == "official":
        if not worker.docker_ready:
            return False
        if worker.gatk_image != exp.gatk_image or worker.happy_image != exp.happy_image:
            return False
        if (worker.gatk_image_id or "") != exp.gatk_image_id:
            return False
    return True


def _validation_set(trials: list[Trial], top_n: int) -> list[Trial]:
    completed = [trial for trial in trials if trial.state == "COMPLETED" and trial.objective is not None]
    completed.sort(key=lambda trial: (-float(trial.objective or 0), trial.config_sha256))
    picked: list[Trial] = []
    seen: set[str] = set()
    for trial in completed:
        if trial.config_sha256 in seen:
            continue
        picked.append(trial)
        seen.add(trial.config_sha256)
        if len(picked) >= top_n:
            break
    for trial in trials:
        if trial.is_baseline and trial.state == "COMPLETED" and trial.id not in {item.id for item in picked}:
            picked.append(trial)
    return picked


def _held_out_mean(jobs: list[Job], trial_id: str, execution_mode: str) -> float | None:
    owned = [job for job in jobs if job.trial_id == trial_id and job.role == "held_out" and job.state == "COMPLETED"]
    if not owned:
        return None
    scores = []
    for job in owned:
        value = job.official_score if execution_mode == "official" else job.mock_objective
        if value is None:
            return None
        scores.append(float(value))
    return sum(scores) / len(scores)


def _job_payload(job: Job, exp: Experiment, dataset: Dataset, token: str) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "lease_token": token,
        "experiment_id": exp.id,
        "experiment_name": exp.name,
        "trial_id": job.trial_id,
        "dataset_id": job.dataset_id,
        "role": job.role,
        "region": dataset.region,
        "config": _loads(job.config_json, {}),
        "config_sha256": job.config_sha256,
        "timeout_seconds": job.timeout_seconds,
        "threads": job.threads,
        "memory_gb": job.memory_gb,
        "attempt": job.attempt,
        "execution_mode": job.execution_mode,
        "scoring_version": exp.scoring_version,
        "scorer_name": exp.scorer_name,
        "minos_commit": exp.minos_commit,
        "gatk_image": exp.gatk_image,
        "gatk_image_id": exp.gatk_image_id,
        "happy_image": exp.happy_image,
        "bcftools_image": exp.bcftools_image,
        "manifest_sha256": dataset.manifest_sha256,
        "reference_sha256": dataset.reference_sha256,
        "reference_build": dataset.reference_build,
    }


def _export_view(row: ExportRecord) -> dict[str, Any]:
    return {
        "export_id": row.id,
        "experiment_id": row.experiment_id,
        "created_at": row.created_at,
        "actor": row.actor,
        "content_sha256": row.content_sha256,
        "research_only": row.research_only,
        "warning": row.warning,
        "downloads": {
            "conf": f"/api/v1/exports/{row.id}/download?part=conf",
            "provenance": f"/api/v1/exports/{row.id}/download?part=provenance",
        },
    }


def _worker_view(worker: Worker) -> dict[str, Any]:
    return {
        "worker_id": worker.id,
        "hostname": worker.hostname,
        "host_os": worker.host_os,
        "platform": worker.platform,
        "cpu_count": worker.cpu_count,
        "ram_mb": worker.ram_mb,
        "free_disk_mb": worker.free_disk_mb,
        "max_parallel_jobs": worker.max_parallel_jobs,
        "docker_ready": worker.docker_ready,
        "gatk_image": worker.gatk_image,
        "gatk_image_id": worker.gatk_image_id,
        "happy_image": worker.happy_image,
        "scorer_version": worker.scorer_version,
        "scorer_name": worker.scorer_name,
        "minos_commit": worker.minos_commit,
        "execution_mode": worker.execution_mode,
        "cached_dataset_ids": _loads(worker.cached_datasets, []),
        "cpu_percent": worker.cpu_percent,
        "ram_used_mb": worker.ram_used_mb,
        "disk_total_mb": worker.disk_total_mb,
        "last_heartbeat": worker.last_heartbeat,
        "scheduling_mode": worker.scheduling_mode or "enabled",
        "policy_max_parallel": worker.policy_max_parallel,
        "effective_max_parallel": effective_parallel(worker),
    }


def _dataset_view(dataset: Dataset, file_count: int | None = None) -> dict[str, Any]:
    view = {
        "dataset_id": dataset.id,
        "name": dataset.name,
        "region": dataset.region,
        "chromosome": dataset.chromosome,
        "reference_build": dataset.reference_build,
        "manifest_sha256": dataset.manifest_sha256,
        "reference_sha256": dataset.reference_sha256,
        "scoring_version": dataset.scoring_version,
        "total_bytes": dataset.total_bytes,
        "source": dataset.source,
        "license_note": dataset.license_note,
        "created_at": dataset.created_at,
    }
    if file_count is not None:
        view["file_count"] = file_count
    return view


def _public_manifest(dataset: Dataset, files: list[DatasetFile]) -> dict[str, Any]:
    return {
        "dataset_id": dataset.id,
        "name": dataset.name,
        "region": dataset.region,
        "chromosome": dataset.chromosome,
        "reference_build": dataset.reference_build,
        "reference_header": dataset.reference_header,
        "manifest_sha256": dataset.manifest_sha256,
        "reference_sha256": dataset.reference_sha256,
        "scoring_version": dataset.scoring_version,
        "source": dataset.source,
        "license_note": dataset.license_note,
        "files": [
            {
                "role": item.role,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in files
        ],
    }


# The attribute below keeps finalize readable without a second query helper on Trial.
def _counts_training_pending(self: Trial, jobs: list[Job]) -> bool:
    return any(job.trial_id == self.id and job.counts_toward_objective and job.state in ACTIVE for job in jobs)


Trial.counts_training_pending = _counts_training_pending  # type: ignore[attr-defined]
