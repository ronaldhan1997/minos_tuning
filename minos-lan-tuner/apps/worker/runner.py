"""Claim, cache, execute, and report. One trial stays on one worker."""

from __future__ import annotations

import base64
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from apps.worker.cache import CacheError, cached_dataset_ids, ensure_dataset, role_paths
from apps.worker.doctor import disk_free_mb, host_usage, ram_mb
from apps.worker.image_pull import pull_image
from apps.worker.settings import WorkerSettings
from integrations.minos.engine import run_mock, run_official
from integrations.minos.pin import (
    bcftools_image,
    docker_executable,
    gatk_image,
    happy_image,
    head_commit,
    inspect_image_id,
    run_docker,
    scorer_name_for,
)
from shared.atomic import atomic_write_json
from shared.schemas import ClaimRequest, CompleteRequest, FailRequest, Heartbeat, ReleaseRequest, WorkerRegister

log = logging.getLogger("minos.worker")
_job_lock = threading.Lock()
_current_job_id: str | None = None
_current_log: str | None = None


def make_client(settings: WorkerSettings, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "base_url": settings.coordinator_url,
        "headers": {"Authorization": f"Bearer {settings.worker_token}"},
        "timeout": httpx.Timeout(60.0, read=600.0),
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def identity(settings: WorkerSettings) -> WorkerRegister:
    image_id = inspect_image_id(gatk_image())
    memory = ram_mb()
    if memory < 1:
        memory = 1
    return WorkerRegister(
        worker_id=settings.worker_id,
        hostname=os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "worker"),
        host_os=sys.platform,
        platform=sys.platform,
        cpu_count=os.cpu_count() or 1,
        ram_mb=memory,
        free_disk_mb=disk_free_mb(settings.cache_dir),
        max_parallel_jobs=settings.max_concurrent_jobs,
        docker_ready=image_id is not None,
        gatk_image=gatk_image(),
        gatk_image_id=image_id,
        happy_image=happy_image(),
        scorer_version=settings.scoring_version,  # type: ignore[arg-type]
        scorer_name=scorer_name_for(settings.scoring_version),
        minos_commit=head_commit(),
        execution_mode=settings.execution_mode,  # type: ignore[arg-type]
    )


def register(client: httpx.Client, settings: WorkerSettings) -> None:
    response = client.post("/api/v1/workers/register", json=identity(settings).model_dump())
    if response.status_code != 200:
        raise RuntimeError(f"register failed: HTTP {response.status_code} {response.text[:300]}")


def run_once(
    client: httpx.Client,
    settings: WorkerSettings,
    *,
    stop_event: threading.Event | None = None,
    wait_seconds: int = 0,
) -> bool:
    """Handle at most one job. Returns True when a job was claimed."""
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    if (settings.work_dir / "worker.stop").exists():
        return False
    _flush_outbox(client, settings)
    if stop_event is not None and stop_event.is_set():
        return False
    _heartbeat(client, settings, [])
    response = client.post(
        "/api/v1/jobs/claim",
        params={"wait_seconds": wait_seconds},
        json=ClaimRequest(
            cached_dataset_ids=cached_dataset_ids(settings.cache_dir),
            execution_mode=settings.execution_mode,  # type: ignore[arg-type]
            free_disk_mb=disk_free_mb(settings.cache_dir),
            running_jobs=0,
        ).model_dump(),
    )
    if response.status_code == 204:
        return False
    if response.status_code != 200:
        raise RuntimeError(f"claim failed: HTTP {response.status_code} {response.text[:300]}")
    job = response.json()
    _execute(client, settings, job, stop_event or threading.Event())
    _write_status(settings, job["job_id"])
    return True


def adopt_server_config(settings: WorkerSettings, body: dict[str, Any]) -> WorkerSettings:
    mode = str(body.get("execution_mode") or "")
    version = str(body.get("scoring_version") or "")
    if mode not in {"mock", "official"} or version not in {"v1", "v2"}:
        raise RuntimeError("server config is missing the score or the run type")
    return replace(settings, execution_mode=mode, scoring_version=version)


_pull_not_before = 0.0
_image_block_reason = "This computer cannot run the official study until Docker has the GATK image."
_status_hook: Any = None


def set_status_hook(hook: Any) -> None:
    """Show worker progress in the window. The console still prints the same line."""
    global _status_hook
    _status_hook = hook


def _docker_engine_unavailable(detail: str) -> bool:
    text = detail.lower()
    return any(
        phrase in text
        for phrase in ("docker is not running", "no such file", "connection refused", "pipe", "timed out")
    )


def _announce(message: str) -> None:
    print(message, flush=True)
    hook = _status_hook
    if hook is not None:
        hook(message)


def ensure_study_images(images: list[str]) -> bool:
    """Download any pinned study image that is not already on this computer."""
    global _pull_not_before, _image_block_reason
    if images and all(inspect_image_id(image) for image in images):
        return True
    now = time.time()
    if now < _pull_not_before:
        return False
    if docker_executable() is None:
        _pull_not_before = now + 60
        _image_block_reason = (
            "Docker Desktop is not running. Start it and leave this window open. "
            "The study downloads the GATK image after Docker answers."
        )
        return False
    for image in images:
        if inspect_image_id(image):
            continue
        _announce(f"Downloading {image}")
        ok, detail = pull_image(image, _announce)
        if not ok and _docker_engine_unavailable(detail):
            result = run_docker(["pull", image], timeout=3600)
            ok = result is not None and result.returncode == 0
            if not ok and result is not None and result.stderr:
                lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
                if lines:
                    detail = " " + lines[-1][:200]
        if not ok or inspect_image_id(image) is None:
            extra = detail if detail.startswith(" ") else f" {detail}" if detail else ""
            _image_block_reason = f"Could not download {image}.{extra}".rstrip(".")
            _pull_not_before = time.time() + 60
            return False
    if not all(inspect_image_id(image) for image in images):
        _image_block_reason = "Docker answered, but the GATK image is still missing."
        _pull_not_before = time.time() + 60
        return False
    return True


def sync_server_config(
    client: httpx.Client,
    settings: WorkerSettings,
    applied: tuple[str, str] | None,
) -> tuple[WorkerSettings, tuple[str, str] | None]:
    """Download the active run and register as that score. Returns the settings to claim with."""
    response = client.get("/api/v1/workers/config")
    if response.status_code != 200:
        raise RuntimeError(f"config download failed: HTTP {response.status_code} {response.text[:300]}")
    body = response.json()
    if not body.get("available"):
        if applied is None:
            register(client, settings)
            return settings, (settings.execution_mode, settings.scoring_version)
        return settings, applied
    mode = str(body["execution_mode"])
    version = str(body["scoring_version"])
    if mode == "official":
        images = [
            str(body.get("gatk_image") or gatk_image()),
            str(body.get("happy_image") or happy_image()),
            str(body.get("bcftools_image") or bcftools_image()),
        ]
        if not ensure_study_images(images):
            if applied != ("blocked", mode):
                _announce(_image_block_reason)
                if applied is None:
                    register(client, settings)
            return settings, ("blocked", mode)
        wanted_id = str(body.get("gatk_image_id") or "")
        local_id = inspect_image_id(gatk_image()) or ""
        if wanted_id and local_id != wanted_id:
            if applied != ("mismatch", mode):
                _announce("The GATK image on this computer does not match the study.")
                if applied is None:
                    register(client, settings)
            return settings, ("mismatch", mode)
    updated = adopt_server_config(settings, body)
    key = (updated.execution_mode, updated.scoring_version)
    if key != applied:
        register(client, updated)
        name = body.get("experiment_name") or "the server"
        _announce(f"Using {mode} score {version} from {name}.")
    return updated, key


def serve(
    settings: WorkerSettings,
    client: httpx.Client,
    *,
    stop_event: threading.Event | None = None,
    max_loops: int | None = None,
) -> None:
    stop = stop_event or threading.Event()
    _install_signals(stop)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    stop_file = settings.work_dir / "worker.stop"
    stop_file.unlink(missing_ok=True)
    (settings.work_dir / "worker.pid").write_text(str(os.getpid()), encoding="utf-8")
    host_usage(settings.cache_dir)
    time.sleep(0.25)
    presence = threading.Thread(
        target=_presence_loop,
        args=(settings, stop),
        name="minos-presence",
        daemon=True,
    )
    presence.start()
    applied: tuple[str, str] | None = None
    loops = 0
    try:
        while not stop.is_set() and not stop_file.exists():
            if max_loops is not None and loops >= max_loops:
                break
            settings, applied = sync_server_config(client, settings, applied)
            did = run_once(client, settings, stop_event=stop, wait_seconds=0 if max_loops is not None else 5)
            loops += 1
            if not did:
                time.sleep(0.2 if max_loops is not None else 1)
    finally:
        pid = settings.work_dir / "worker.pid"
        if pid.exists() and pid.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid.unlink(missing_ok=True)


def request_stop(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "worker.stop").write_text("stop\n", encoding="utf-8")
    pid_path = work_dir / "worker.pid"
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.warning("could not signal worker pid %s: %s", pid, exc)


def _set_current_job(job_id: str | None) -> None:
    global _current_job_id, _current_log
    with _job_lock:
        _current_job_id = job_id
        if job_id is None:
            _current_log = None


def _presence_snapshot() -> tuple[str | None, str | None]:
    with _job_lock:
        return _current_job_id, _current_log


def _remember_log(job_id: str, text: str | None) -> None:
    global _current_log
    with _job_lock:
        if _current_job_id == job_id:
            _current_log = text


def _presence_loop(settings: WorkerSettings, stop: threading.Event, interval: float = 10.0) -> None:
    """Check in on its own. A Docker call must not be able to stop the next check-in."""
    while not stop.wait(interval):
        job_id, excerpt = _presence_snapshot()
        try:
            with make_client(settings) as client:
                _heartbeat(
                    client,
                    settings,
                    [] if job_id is None else [job_id],
                    log_excerpt=excerpt,
                    timeout=15.0,
                )
        except Exception:
            log.warning("presence report failed", exc_info=True)


def _execute(client: httpx.Client, settings: WorkerSettings, job: dict[str, Any], stop_event: threading.Event) -> None:
    _set_current_job(job["job_id"])
    sample_stop = threading.Event()
    sampler = threading.Thread(
        target=_sample_job_log,
        args=(job["job_id"], sample_stop),
        name="minos-job-log",
        daemon=True,
    )
    sampler.start()
    try:
        _execute_job(client, settings, job, stop_event)
    finally:
        sample_stop.set()
        _set_current_job(None)


def _sample_job_log(job_id: str, stop: threading.Event) -> None:
    """Refresh the cached container log. This thread may wait on Docker."""
    while not stop.wait(15):
        try:
            _remember_log(job_id, _job_container_log(job_id))
        except Exception:
            log.warning("job log sample failed", exc_info=True)


def _job_container_log(job_id: str) -> str | None:
    """Latest output from the container whose files belong to this job."""
    listed = run_docker(
        ["ps", "--filter", "name=minos-", "--format", "{{.Names}}"],
        timeout=5,
        capture_stdout=True,
    )
    if listed is None or listed.returncode != 0:
        return None
    chunks: list[str] = []
    for name in (listed.stdout or "").splitlines():
        name = name.strip()
        if not name.startswith("minos-"):
            continue
        inspected = run_docker(
            ["inspect", "--format", "{{range .Mounts}}{{.Source}}\n{{end}}", name],
            timeout=5,
            capture_stdout=True,
        )
        if inspected is None or job_id not in (inspected.stdout or ""):
            continue
        logs = run_docker(["logs", "--tail", "80", name], timeout=5, capture_stdout=True)
        if logs is None:
            continue
        text = "\n".join(part for part in ((logs.stdout or "").strip(), (logs.stderr or "").strip()) if part)
        if text:
            chunks.append(text)
    if not chunks:
        return None
    return "\n".join(chunks)[-8000:]


def _execute_job(client: httpx.Client, settings: WorkerSettings, job: dict[str, Any], stop_event: threading.Event) -> None:
    rejection = _reject_reason(settings, job)
    if rejection:
        register(client, settings)
        _post_or_box(
            client,
            settings,
            "release",
            job["job_id"],
            ReleaseRequest(lease_token=job["lease_token"], message=rejection).model_dump(),
        )
        return
    if settings.execution_mode == "mock":
        try:
            ensure_dataset(
                client,
                settings.cache_dir,
                job["dataset_id"],
                cache_max_bytes=settings.cache_max_bytes,
            )
        except CacheError as exc:
            _fail(client, settings, job, "cache_error", str(exc), retryable=True)
            return
        result = run_mock(config_sha256=job["config_sha256"], dataset_id=job["dataset_id"])
        _post_or_box(
            client,
            settings,
            "complete",
            job["job_id"],
            CompleteRequest(
                lease_token=job["lease_token"],
                config_sha256=job["config_sha256"],
                score_source="mock",
                official_score=None,
                mock_objective=result.mock_objective,
                elapsed_seconds=0,
                provenance=_provenance(settings, job),
            ).model_dump(),
        )
        return

    try:
        root, manifest = ensure_dataset(
            client,
            settings.cache_dir,
            job["dataset_id"],
            cache_max_bytes=settings.cache_max_bytes,
        )
        roles = role_paths(root, manifest)
        if manifest.get("manifest_sha256") != job["manifest_sha256"]:
            raise CacheError("manifest hash does not match the leased job")
        if manifest.get("region") != job["region"]:
            raise CacheError("dataset region does not match the leased job")
    except CacheError as exc:
        _fail(client, settings, job, "cache_error", str(exc), retryable=True)
        return

    output = settings.work_dir / job["job_id"] / "output.vcf.gz"
    result = run_official(
        bam=roles["bam"],
        reference=roles["reference"],
        output_vcf=output,
        region=job["region"],
        gatk_options=job["config"],
        threads=int(job["threads"]),
        memory_gb=int(job["memory_gb"]),
        timeout_seconds=int(job["timeout_seconds"]),
        truth_vcf=roles["truth"],
        sdf=roles["sdf"],
        mutations_vcf=roles["mutations"],
        scoring_version=job["scoring_version"],
        stop_event=stop_event,
    )
    if result.ok and result.official_score is not None:
        _post_or_box(
            client,
            settings,
            "complete",
            job["job_id"],
            CompleteRequest(
                lease_token=job["lease_token"],
                config_sha256=job["config_sha256"],
                score_source="official",
                official_score=result.official_score,
                mock_objective=None,
                metrics=result.metrics,
                elapsed_seconds=float(result.elapsed_seconds or 0),
                provenance=_provenance(settings, job),
                variant_count=result.variant_count,
            ).model_dump(),
        )
        return
    _fail(
        client,
        settings,
        job,
        result.error_class or "gatk_failed",
        result.message or "execution failed",
        retryable=result.retryable,
        details=result.metrics,
        log_excerpt=result.log_excerpt,
        elapsed=result.elapsed_seconds,
        happy_vcf_b64=_happy_vcf_for_server(result.error_class, result.metrics),
    )


def _reject_reason(settings: WorkerSettings, job: dict[str, Any]) -> str | None:
    if job["execution_mode"] != settings.execution_mode:
        return "execution mode does not match this worker"
    if job["scoring_version"] != settings.scoring_version:
        return "scoring version does not match this worker"
    if job["minos_commit"] != head_commit():
        return "job commit does not match this checkout"
    if job["gatk_image"] != gatk_image() or job["happy_image"] != happy_image() or job["bcftools_image"] != bcftools_image():
        return "job images do not match the pinned templates"
    if int(job["timeout_seconds"]) > settings.max_timeout_seconds:
        return "job timeout exceeds this worker's cap"
    if int(job["memory_gb"]) > settings.max_memory_gb:
        return "job memory exceeds this worker's cap"
    if settings.execution_mode == "official":
        image_id = inspect_image_id(gatk_image())
        if image_id != job["gatk_image_id"]:
            return f"local GATK image id {image_id} does not match the study pin {job['gatk_image_id']}"
    return None


def _happy_vcf_for_server(error_class: str | None, metrics: dict[str, Any] | None) -> str | None:
    """Send the hap.py VCF when this process cannot import pysam.

    Linux clients score v2 themselves. Windows cannot install pysam, so the
    coordinator reads the same file with the official parser.
    """
    if error_class != "scoring_unavailable":
        return None
    from integrations.minos.scoring import _pysam_importable

    if _pysam_importable():
        return None
    path = (metrics or {}).get("happy_vcf_path")
    if not path:
        return None
    file = Path(str(path))
    try:
        if not file.is_file() or file.stat().st_size <= 0 or file.stat().st_size > 8 * 1024 * 1024:
            return None
        return base64.b64encode(file.read_bytes()).decode("ascii")
    except OSError:
        return None


def _fail(
    client: httpx.Client,
    settings: WorkerSettings,
    job: dict[str, Any],
    error_class: str,
    message: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
    log_excerpt: str | None = None,
    elapsed: float | None = None,
    happy_vcf_b64: str | None = None,
) -> None:
    _post_or_box(
        client,
        settings,
        "fail",
        job["job_id"],
        FailRequest(
            lease_token=job["lease_token"],
            error_class=error_class,
            message=message[:4000],
            retryable=retryable,
            elapsed_seconds=elapsed,
            log_excerpt=None if log_excerpt is None else log_excerpt[:8000],
            details=details or {},
            happy_vcf_b64=happy_vcf_b64,
        ).model_dump(),
    )


def _provenance(settings: WorkerSettings, job: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_id": settings.worker_id,
        "platform": sys.platform,
        "execution_mode": settings.execution_mode,
        "scoring_version": job["scoring_version"],
        "scorer_name": job["scorer_name"],
        "minos_commit": job["minos_commit"],
        "gatk_image": job["gatk_image"],
        "gatk_image_id": job["gatk_image_id"],
        "happy_image": job["happy_image"],
        "bcftools_image": job["bcftools_image"],
        "threads": job["threads"],
        "memory_gb": job["memory_gb"],
        "dataset_id": job["dataset_id"],
        "manifest_sha256": job["manifest_sha256"],
        "reference_sha256": job["reference_sha256"],
    }


def _heartbeat(
    client: httpx.Client,
    settings: WorkerSettings,
    active: list[str],
    log_excerpt: str | None = None,
    timeout: float | None = None,
) -> None:
    usage = host_usage(settings.cache_dir)
    extra: dict[str, Any] = {}
    if timeout is not None:
        extra["timeout"] = httpx.Timeout(timeout)
    response = client.post(
        "/api/v1/workers/heartbeat",
        **extra,
        json=Heartbeat(
            active_job_ids=active,
            cpu_percent=usage["cpu_percent"] if isinstance(usage["cpu_percent"], float) else None,
            ram_used_mb=int(usage["ram_used_mb"] or 0),
            free_disk_mb=int(usage["free_disk_mb"] or 0),
            disk_total_mb=int(usage["disk_total_mb"] or 0),
            cached_dataset_ids=cached_dataset_ids(settings.cache_dir),
            log_excerpt=None if log_excerpt is None else log_excerpt[-8000:],
        ).model_dump(),
    )
    if response.status_code != 200:
        log.warning("heartbeat HTTP %s", response.status_code)


def _post_or_box(client: httpx.Client, settings: WorkerSettings, kind: str, job_id: str, body: dict[str, Any]) -> None:
    path = {"complete": "complete", "fail": "fail", "release": "release"}[kind]
    try:
        response = client.post(f"/api/v1/jobs/{job_id}/{path}", json=body)
    except httpx.HTTPError as exc:
        log.warning("result upload failed, leaving it in the outbox: %s", exc)
        _box(settings, kind, job_id, body)
        return
    if response.status_code == 200:
        return
    if response.status_code == 409:
        log.warning("coordinator rejected %s for %s as stale", kind, job_id)
        return
    log.warning("coordinator returned HTTP %s for %s; keeping the outbox copy", response.status_code, kind)
    _box(settings, kind, job_id, body)


def _box(settings: WorkerSettings, kind: str, job_id: str, body: dict[str, Any]) -> None:
    atomic_write_json(settings.work_dir / "outbox" / f"{job_id}.json", {"kind": kind, "job_id": job_id, "body": body})


def _flush_outbox(client: httpx.Client, settings: WorkerSettings) -> None:
    outbox = settings.work_dir / "outbox"
    if not outbox.exists():
        return
    for path in sorted(outbox.glob("*.json")):
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            response = client.post(f"/api/v1/jobs/{payload['job_id']}/{payload['kind']}", json=payload["body"])
        except httpx.HTTPError:
            return
        if response.status_code in {200, 409}:
            path.unlink(missing_ok=True)


def _write_status(settings: WorkerSettings, job_id: str) -> None:
    atomic_write_json(
        settings.work_dir / "worker_status.json",
        {"worker_id": settings.worker_id, "last_job_id": job_id, "platform": sys.platform},
    )


def _install_signals(stop: threading.Event) -> None:
    def _handler(_signum: int, _frame: object) -> None:
        stop.set()

    try:
        signal.signal(signal.SIGINT, _handler)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # The Windows window runs the worker off the UI thread, which cannot own signals.
        return
