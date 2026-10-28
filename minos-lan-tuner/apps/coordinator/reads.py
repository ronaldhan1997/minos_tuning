"""Dashboard queries. They do not create trials or tell Optuna."""

from __future__ import annotations

from collections import Counter
from typing import Any

import optuna
import yaml
from optuna.trial import TrialState
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from apps.coordinator.clock import utc_now
from apps.coordinator.errors import ServiceError
from apps.coordinator.live_log import local_container_log
from apps.coordinator.present import (
    HEARTBEAT_STALE_SECONDS,
    best_so_far,
    decode_cursor,
    encode_cursor,
    eta_seconds,
    heartbeat_age_seconds,
    hour_cutoff,
    presence_status,
    public_mapping,
    runtime_note,
)
from apps.coordinator.service import Coordinator, _loads, _worker_view, effective_parallel
from database.models import AuditEvent, Dataset, DatasetFile, Experiment, Job, JobEvent, Trial, Worker
from integrations.minos.pin import (
    PINNED_COMMIT,
    bcftools_image,
    gatk_image,
    happy_image,
    head_commit,
    inspect_image_id,
    scorer_name_for,
)
from integrations.minos.search import default_search_path

_ACTIVE = ("LEASED", "RUNNING")
_PREFERENCE = ("RUNNING", "VALIDATING", "STOPPING", "PAUSED", "DRAFT")


class DashboardReads:
    def __init__(self, coordinator: Coordinator) -> None:
        self.coord = coordinator

    def system_status(self) -> dict[str, Any]:
        self.coord.ready()
        try:
            commit = head_commit()
        except RuntimeError as exc:
            commit = f"unreadable: {exc}"
        return {
            "api_version": "0.1.0",
            "minos_commit": commit,
            "pinned_commit": PINNED_COMMIT,
            "commit_matches_pin": commit == PINNED_COMMIT,
            "gatk_image": gatk_image(),
            "gatk_image_id": inspect_image_id(gatk_image()),
            "happy_image": happy_image(),
            "bcftools_image": bcftools_image(),
            "scorer_versions": ["v1", "v2"],
            "scorer_names": {"v1": scorer_name_for("v1"), "v2": scorer_name_for("v2")},
            "database": "ready",
            "heartbeat_stale_seconds": HEARTBEAT_STALE_SECONDS,
            "server_time": _iso_now(),
            "live_submit": False,
            "http_note": (
                "Plain HTTP is a closed-network tradeoff. Terminate TLS at a LAN reverse proxy "
                "before using this dashboard on a network you do not trust."
            ),
        }

    def overview(self) -> dict[str, Any]:
        now = utc_now()
        with self.coord._db() as db:
            workers = list(db.scalars(select(Worker)))
            experiments = list(db.scalars(select(Experiment).order_by(Experiment.updated_at.desc())))
            jobs = list(db.scalars(select(Job)))
            active_exp = _pick_active(experiments)
            online = 0
            views = []
            warnings: list[dict[str, str]] = []
            pinned = _safe_commit()
            for worker in workers:
                active_jobs = sum(1 for job in jobs if job.worker_id == worker.id and job.state in _ACTIVE)
                status = presence_status(
                    last_heartbeat=worker.last_heartbeat,
                    execution_mode=worker.execution_mode,
                    docker_ready=worker.docker_ready,
                    minos_commit=worker.minos_commit,
                    pinned_commit=pinned,
                    scheduling_mode=worker.scheduling_mode or "enabled",
                    active_jobs=active_jobs,
                    now=now,
                )
                if status not in {"offline", "unknown"}:
                    online += 1
                if status == "incompatible":
                    warnings.append(
                        {
                            "code": "worker_incompatible",
                            "message": f"{worker.id} is online but will not receive jobs until Docker and the pinned commit match.",
                        }
                    )
                if worker.free_disk_mb < 2048:
                    warnings.append(
                        {
                            "code": "low_disk",
                            "message": f"{worker.id} last reported {worker.free_disk_mb} MiB free.",
                        }
                    )
                views.append(status)
            job_counts = Counter(job.state for job in jobs)
            trials_told = 0
            budget = 0
            if active_exp is not None:
                trials = list(db.scalars(select(Trial).where(Trial.experiment_id == active_exp.id)))
                trials_told = sum(1 for trial in trials if trial.told)
                budget = active_exp.budget
            validated = _best_validated(experiments)
            training = _best_training(experiments)
            active_view = None if active_exp is None else self.coord._experiment_view(db, active_exp)
            if training is None and active_view and active_view.get("best_objective") is not None:
                training = {
                    "experiment_id": active_view["id"],
                    "experiment_name": active_view["name"],
                    "training_objective": active_view["best_objective"],
                    "score_source": active_view.get("best_score_source") or active_view["execution_mode"],
                    "scoring_version": active_view["scoring_version"],
                    "validated": False,
                }
            recent = []
            for job in sorted(jobs, key=lambda item: item.updated_at, reverse=True):
                if not job.error_class:
                    continue
                recent.append(
                    {
                        "job_id": job.id,
                        "experiment_id": job.experiment_id,
                        "dataset_id": job.dataset_id,
                        "state": job.state,
                        "error_class": job.error_class,
                        "message": (job.error_message or "")[:180],
                        "updated_at": job.updated_at,
                    }
                )
                if len(recent) == 8:
                    break
            for job in recent:
                if job["error_class"] in {"lease_expired", "scoring_unavailable", "not_recorded"}:
                    warnings.append(
                        {
                            "code": job["error_class"],
                            "message": f"Job {job['job_id']} {job['error_class']}: {job['message']}",
                        }
                    )
            return {
                "server_time": _iso_now(now),
                "heartbeat_stale_seconds": HEARTBEAT_STALE_SECONDS,
                "active_experiment": active_view,
                "experiments": [
                    {"id": exp.id, "name": exp.name, "status": exp.status, "updated_at": exp.updated_at}
                    for exp in experiments[:12]
                ],
                "workers": {"online": online, "total": len(workers)},
                "jobs": {
                    "active": job_counts["LEASED"] + job_counts["RUNNING"],
                    "queued": job_counts["PENDING"],
                    "failed": job_counts["FAILED"] + job_counts["TIMED_OUT"],
                    "completed": job_counts["COMPLETED"],
                },
                "trials": {
                    "finished": trials_told,
                    "budget": budget,
                    "experiment_id": None if active_exp is None else active_exp.id,
                    "note": "Finished counts Optuna trials that have been told, including failures. It is not a job count.",
                },
                "best_validated": validated,
                "best_training": training,
                "recent_errors": recent,
                "warnings": warnings[:12],
                "permissions": {"role": "admin", "can_control": True},
                "worker_presence": dict(Counter(views)),
            }

    def workers(self, *, limit: int, cursor: str | None, q: str | None) -> dict[str, Any]:
        limit = _limit(limit)
        with self.coord._db() as db:
            stmt = select(Worker).order_by(Worker.updated_at.desc(), Worker.id.desc())
            if cursor:
                created, row_id = decode_cursor(cursor)
                stmt = stmt.where(or_(Worker.updated_at < created, and_(Worker.updated_at == created, Worker.id < row_id)))
            if q:
                needle = f"%{q.lower()}%"
                stmt = stmt.where(
                    or_(
                        func.lower(Worker.id).like(needle),
                        func.lower(Worker.hostname).like(needle),
                        func.lower(Worker.platform).like(needle),
                    )
                )
            rows = list(db.scalars(stmt.limit(limit + 1)))
            page, next_cursor = _trim(rows, limit, lambda row: (row.updated_at, row.id))
            return {
                "items": [self._worker(db, row) for row in page],
                "next_cursor": next_cursor,
            }

    def worker(self, worker_id: str) -> dict[str, Any]:
        with self.coord._db() as db:
            row = db.get(Worker, worker_id)
            if row is None:
                raise ServiceError(404, "not_found", "worker was not found")
            view = self._worker(db, row)
            view["recent_jobs"] = [
                _job_summary(job)
                for job in db.scalars(
                    select(Job).where(Job.worker_id == worker_id).order_by(Job.updated_at.desc()).limit(20)
                )
            ]
            return view

    def jobs(
        self,
        *,
        limit: int,
        cursor: str | None,
        experiment_id: str | None,
        worker_id: str | None,
        dataset_id: str | None,
        state: str | None,
        trial_id: str | None,
        q: str | None,
    ) -> dict[str, Any]:
        limit = _limit(limit)
        with self.coord._db() as db:
            stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
            if experiment_id:
                stmt = stmt.where(Job.experiment_id == experiment_id)
            if worker_id:
                stmt = stmt.where(Job.worker_id == worker_id)
            if dataset_id:
                stmt = stmt.where(Job.dataset_id == dataset_id)
            if state:
                stmt = stmt.where(Job.state == state)
            if trial_id:
                stmt = stmt.where(Job.trial_id == trial_id)
            if cursor:
                created, row_id = decode_cursor(cursor)
                stmt = stmt.where(or_(Job.created_at < created, and_(Job.created_at == created, Job.id < row_id)))
            if q:
                needle = f"%{q.lower()}%"
                stmt = stmt.where(
                    or_(
                        func.lower(Job.id).like(needle),
                        func.lower(Job.dataset_id).like(needle),
                        func.lower(func.coalesce(Job.error_class, "")).like(needle),
                        func.lower(func.coalesce(Job.worker_id, "")).like(needle),
                    )
                )
            rows = list(db.scalars(stmt.limit(limit + 1)))
            page, next_cursor = _trim(rows, limit, lambda row: (row.created_at, row.id))
            return {"items": [_job_summary(job) for job in page], "next_cursor": next_cursor}

    def job(self, job_id: str) -> dict[str, Any]:
        with self.coord._db() as db:
            job = db.get(Job, job_id)
            if job is None:
                raise ServiceError(404, "not_found", "job was not found")
            dataset = db.get(Dataset, job.dataset_id)
            events = list(
                db.scalars(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.at))
            )
            summary = _job_summary(job)
            summary["error_message"] = None if job.error_message is None else job.error_message[:4000]
            summary.update(
                {
                    "config": _loads(job.config_json, {}),
                    "metrics": _loads(job.metrics_json, {}),
                    "provenance": public_mapping(_loads(job.provenance_json, {})),
                    "dataset": None
                    if dataset is None
                    else {
                        "dataset_id": dataset.id,
                        "name": dataset.name,
                        "region": dataset.region,
                        "chromosome": dataset.chromosome,
                        "reference_build": dataset.reference_build,
                        "manifest_sha256": dataset.manifest_sha256,
                        "reference_sha256": dataset.reference_sha256,
                        "scoring_version": dataset.scoring_version,
                    },
                    "events": [
                        {"state": event.state, "note": event.note, "at": event.at} for event in events
                    ],
                    "log_available": bool(job.log_excerpt),
                    "lease_expires_at": job.lease_expires_at,
                    "cache_key": job.cache_key,
                }
            )
            return summary

    def job_log(self, job_id: str, tail: int) -> dict[str, Any]:
        tail = max(1, min(tail, 8000))
        with self.coord._db() as db:
            job = db.get(Job, job_id)
            if job is None:
                raise ServiceError(404, "not_found", "job was not found")
            text = job.log_excerpt or ""
            clipped = text[-tail:]
            return {
                "job_id": job.id,
                "excerpt": clipped,
                "truncated": len(text) > len(clipped),
                "source": None if not text else "worker_failure_excerpt",
                "note": None
                if text
                else "The worker did not upload a log for this job. Successful runs do not attach stdout.",
            }

    def datasets(self) -> dict[str, Any]:
        with self.coord._db() as db:
            rows = list(db.scalars(select(Dataset).order_by(Dataset.created_at)))
            items = []
            for dataset in rows:
                files = list(db.scalars(select(DatasetFile).where(DatasetFile.dataset_id == dataset.id)))
                cached_by = [
                    worker.id
                    for worker in db.scalars(select(Worker))
                    if dataset.id in _loads(worker.cached_datasets, [])
                ]
                items.append(
                    {
                        "dataset_id": dataset.id,
                        "name": dataset.name,
                        "region": dataset.region,
                        "chromosome": dataset.chromosome,
                        "reference_build": dataset.reference_build,
                        "manifest_sha256": dataset.manifest_sha256,
                        "reference_sha256": dataset.reference_sha256,
                        "scoring_version": dataset.scoring_version,
                        "source": dataset.source,
                        "license_note": dataset.license_note,
                        "total_bytes": dataset.total_bytes,
                        "created_at": dataset.created_at,
                        "roles": [item.role for item in files],
                        "workers_reporting_cache": cached_by,
                    }
                )
            return {"items": items}

    def experiment_log(self, key: str) -> dict[str, Any]:
        with self.coord._db() as db:
            exp = self.coord._by_key(db, key)
            jobs = [
                job
                for job in self.coord._jobs(db, exp.id)
                if job.state in {"RUNNING", "LEASED"}
            ]
            jobs.sort(key=lambda job: job.updated_at, reverse=True)
            pending = [
                (job.id, job.worker_id, job.state, (job.log_excerpt or "").strip())
                for job in jobs
            ]
            experiment_id = exp.id
        items = []
        for job_id, worker_id, state, text in pending:
            if not text:
                text = (local_container_log(job_id) or "").strip()
            items.append(
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "state": state,
                    "excerpt": text[-8000:],
                }
            )
        return {"experiment_id": experiment_id, "items": items}

    def metrics(self, key: str, *, include_importance: bool) -> dict[str, Any]:
        with self.coord._db() as db:
            exp = self.coord._by_key(db, key)
            trials = self.coord._trials(db, exp.id)
            jobs = self.coord._jobs(db, exp.id)
            trial_rows = []
            for trial in trials:
                trial_rows.append(
                    {
                        "trial_id": trial.id,
                        "optuna_number": trial.optuna_number,
                        "objective": trial.objective,
                        "told": trial.told,
                        "updated_at": trial.updated_at,
                        "score_source": trial.score_source,
                        "is_baseline": trial.is_baseline,
                        "state": trial.state,
                        "params": _loads(trial.params_json, {}),
                    }
                )
            elapsed = [
                float(job.elapsed_seconds)
                for job in jobs
                if job.state == "COMPLETED" and job.elapsed_seconds is not None
            ]
            mean_elapsed = None if not elapsed else sum(elapsed) / len(elapsed)
            remaining = sum(1 for job in jobs if job.state in {"PENDING", "LEASED", "RUNNING"})
            held_out = []
            for trial in trials:
                scores = _role_scores(jobs, trial.id, "held_out", exp.execution_mode)
                if scores is None:
                    continue
                held_out.append(
                    {
                        "trial_id": trial.id,
                        "optuna_number": trial.optuna_number,
                        "held_out_mean": sum(scores) / len(scores),
                        "score_source": exp.execution_mode,
                        "scoring_version": exp.scoring_version,
                    }
                )
            payload: dict[str, Any] = {
                "experiment_id": exp.id,
                "status": exp.status,
                "scoring_version": exp.scoring_version,
                "scorer_name": exp.scorer_name,
                "execution_mode": exp.execution_mode,
                "score_scale": _score_scale(exp.execution_mode, exp.scoring_version),
                "best_so_far": best_so_far(trial_rows),
                "held_out": held_out,
                "throughput_completed_jobs_last_hour": sum(
                    1
                    for job in jobs
                    if job.state == "COMPLETED" and job.updated_at >= hour_cutoff()
                ),
                "mean_elapsed_seconds": mean_elapsed,
                "failure_counts": dict(Counter(job.error_class for job in jobs if job.error_class)),
                "eta": eta_seconds(elapsed, remaining) if exp.status in {"RUNNING", "VALIDATING", "STOPPING"} else None,
                "candidates": _candidates(trials, jobs, exp.execution_mode),
                "parameter_samples": [
                    {
                        "optuna_number": trial.optuna_number,
                        "objective": trial.objective,
                        "params": _loads(trial.params_json, {}),
                        "score_source": trial.score_source,
                    }
                    for trial in trials
                    if trial.objective is not None
                ],
            }
            study_name = exp.id
        payload["importance"] = (
            _importance(self.coord, study_name) if include_importance else {"available": False, "reason": "not requested"}
        )
        return payload

    def events(self, key: str, limit: int) -> dict[str, Any]:
        limit = _limit(limit)
        with self.coord._db() as db:
            exp = self.coord._by_key(db, key)
            audits = list(
                db.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.target_id.in_([exp.id]))
                    .order_by(AuditEvent.at.desc())
                    .limit(limit)
                )
            )
            return {"items": [_audit_view(row) for row in audits]}

    def audit(self, *, limit: int, cursor: str | None) -> dict[str, Any]:
        limit = _limit(limit)
        with self.coord._db() as db:
            stmt = select(AuditEvent).order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
            if cursor:
                created, row_id = decode_cursor(cursor)
                stmt = stmt.where(or_(AuditEvent.at < created, and_(AuditEvent.at == created, AuditEvent.id < row_id)))
            rows = list(db.scalars(stmt.limit(limit + 1)))
            page, next_cursor = _trim(rows, limit, lambda row: (row.at, row.id))
            return {"items": [_audit_view(row) for row in page], "next_cursor": next_cursor}

    def search_space(self) -> dict[str, Any]:
        path = default_search_path()
        if not path.is_file():
            raise ServiceError(404, "not_found", "search space file is not on the coordinator")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return {"path_name": path.name, "document": document}

    def _worker(self, db: Session, worker: Worker) -> dict[str, Any]:
        active = list(
            db.scalars(
                select(Job).where(Job.worker_id == worker.id, Job.state.in_(_ACTIVE)).order_by(Job.updated_at.desc())
            )
        )
        pinned = _safe_commit()
        status = presence_status(
            last_heartbeat=worker.last_heartbeat,
            execution_mode=worker.execution_mode,
            docker_ready=worker.docker_ready,
            minos_commit=worker.minos_commit,
            pinned_commit=pinned,
            scheduling_mode=worker.scheduling_mode or "enabled",
            active_jobs=len(active),
        )
        view = _worker_view(worker)
        view.update(
            {
                "presence": status,
                "heartbeat_age_seconds": heartbeat_age_seconds(worker.last_heartbeat),
                "stale": status in {"offline", "unknown"},
                "runtime_note": runtime_note(worker.platform, worker.host_os),
                "active_job_ids": [job.id for job in active],
                "effective_max_parallel": effective_parallel(worker),
                "pinned_commit": pinned,
                "commit_matches_coordinator": worker.minos_commit == pinned,
            }
        )
        return view


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "experiment_id": job.experiment_id,
        "trial_id": job.trial_id,
        "dataset_id": job.dataset_id,
        "role": job.role,
        "counts_toward_objective": job.counts_toward_objective,
        "state": job.state,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "worker_id": job.worker_id,
        "official_score": job.official_score,
        "mock_objective": job.mock_objective,
        "score_source": job.score_source,
        "error_class": job.error_class,
        "error_message": None if job.error_message is None else job.error_message[:500],
        "elapsed_seconds": job.elapsed_seconds,
        "timeout_seconds": job.timeout_seconds,
        "threads": job.threads,
        "memory_gb": job.memory_gb,
        "execution_mode": job.execution_mode,
        "config_sha256": job.config_sha256,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _audit_view(row: AuditEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.at,
        "actor": row.actor,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "result": row.result,
        "detail": _loads(row.detail_json, {}),
    }


def _role_scores(jobs: list[Job], trial_id: str, role: str, execution_mode: str) -> list[float] | None:
    owned = [job for job in jobs if job.trial_id == trial_id and job.role == role and job.state == "COMPLETED"]
    if not owned:
        return None
    scores: list[float] = []
    for job in owned:
        value = job.official_score if execution_mode == "official" else job.mock_objective
        if value is None:
            return None
        scores.append(float(value))
    return scores


def _candidates(trials: list[Trial], jobs: list[Job], execution_mode: str) -> list[dict[str, Any]]:
    completed = [trial for trial in trials if trial.state == "COMPLETED" and trial.objective is not None]
    completed.sort(key=lambda trial: (-float(trial.objective or 0), trial.config_sha256))
    picked: list[Trial] = []
    seen: set[str] = set()
    for trial in completed:
        if trial.config_sha256 in seen:
            continue
        picked.append(trial)
        seen.add(trial.config_sha256)
        if len(picked) >= 5:
            break
    for trial in trials:
        if trial.is_baseline and trial.id not in {item.id for item in picked}:
            picked.append(trial)
    rows = []
    for trial in picked:
        scores = _role_scores(jobs, trial.id, "training", execution_mode) or []
        metrics = _metric_means(jobs, trial.id)
        rows.append(
            {
                "trial_id": trial.id,
                "optuna_number": trial.optuna_number,
                "is_baseline": trial.is_baseline,
                "state": trial.state,
                "training_objective": trial.objective,
                "training_mean": None if not scores else sum(scores) / len(scores),
                "training_stdev": _pstdev(scores),
                "worst_training_score": None if not scores else min(scores),
                "sample_count": len(scores),
                "score_source": trial.score_source,
                "config_sha256": trial.config_sha256,
                "f1_snp": metrics.get("f1_snp"),
                "f1_indel": metrics.get("f1_indel"),
                "params": _loads(trial.params_json, {}),
            }
        )
    return rows


def _metric_means(jobs: list[Job], trial_id: str) -> dict[str, float | None]:
    found: dict[str, list[float]] = {"f1_snp": [], "f1_indel": []}
    for job in jobs:
        if job.trial_id != trial_id or job.role != "training" or job.state != "COMPLETED":
            continue
        metrics = _loads(job.metrics_json, {})
        for key in found:
            if key in metrics and metrics[key] is not None:
                found[key].append(float(metrics[key]))
    return {key: (sum(values) / len(values) if values else None) for key, values in found.items()}


def _pstdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return (sum((item - mean) ** 2 for item in values) / len(values)) ** 0.5


def _score_scale(execution_mode: str, scoring_version: str) -> str:
    if execution_mode == "mock":
        return "Mock objective. Not an AdvancedScorer result and not on the 0–100 official scale."
    if scoring_version == "v1":
        return "Official AdvancedScorer v1, 0–100. Not interchangeable with v2."
    return "Official AdvancedScorer v2, 0–100. Not interchangeable with v1."


def _pick_active(experiments: list[Experiment]) -> Experiment | None:
    for status in _PREFERENCE:
        for exp in experiments:
            if exp.status == status:
                return exp
    return experiments[0] if experiments else None


def _best_validated(experiments: list[Experiment]) -> dict[str, Any] | None:
    found: tuple[str, Experiment, float] | None = None
    for exp in experiments:
        if exp.status != "COMPLETED" or not exp.selection_json:
            continue
        selection = _loads(exp.selection_json, {})
        value = selection.get("held_out_mean")
        if value is None:
            continue
        if found is None or exp.updated_at > found[0]:
            found = (exp.updated_at, exp, float(value))
    if found is None:
        return None
    _, exp, value = found
    selection = _loads(exp.selection_json, {})
    return {
        "experiment_id": exp.id,
        "experiment_name": exp.name,
        "held_out_mean": value,
        "score_source": selection.get("score_source") or exp.execution_mode,
        "scoring_version": exp.scoring_version,
        "scorer_name": exp.scorer_name,
        "validated": True,
    }


def _best_training(experiments: list[Experiment]) -> dict[str, Any] | None:
    found: tuple[float, Experiment] | None = None
    for exp in experiments:
        if exp.status not in {"RUNNING", "VALIDATING", "PAUSED", "STOPPING", "COMPLETED", "STOPPED"}:
            continue
        selection = _loads(exp.selection_json, {}) if exp.selection_json else {}
        value = selection.get("training_objective")
        if value is None:
            value = selection.get("best_training_objective")
        if value is None:
            continue
        if found is None or float(value) > found[0]:
            found = (float(value), exp)
    if found is None:
        return None
    value, exp = found
    return {
        "experiment_id": exp.id,
        "experiment_name": exp.name,
        "training_objective": value,
        "score_source": exp.execution_mode,
        "scoring_version": exp.scoring_version,
        "validated": False,
    }


def _importance(coord: Coordinator, study_name: str) -> dict[str, Any]:
    try:
        study = optuna.load_study(study_name=study_name, storage=coord.storage())
    except KeyError:
        return {"available": False, "reason": "Optuna study is not created yet"}
    completed = [trial for trial in study.trials if trial.state == TrialState.COMPLETE and trial.value is not None]
    if len(completed) < 4:
        return {"available": False, "reason": "Need at least 4 completed Optuna trials"}
    try:
        from optuna.importance import get_param_importances

        values = get_param_importances(study)
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:300]}
    return {"available": True, "values": values}


def _safe_commit() -> str:
    try:
        return head_commit()
    except RuntimeError:
        return ""


def _iso_now(moment: Any | None = None) -> str:
    from apps.coordinator.clock import iso

    return iso(moment or utc_now())


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 200))


def _trim(rows: list[Any], limit: int, key) -> tuple[list[Any], str | None]:
    if len(rows) <= limit:
        return rows, None
    page = rows[:limit]
    created, row_id = key(page[-1])
    return page, encode_cursor(created, row_id)
