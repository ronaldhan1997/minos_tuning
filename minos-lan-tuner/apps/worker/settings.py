"""Worker configuration. The token comes from the environment, not the yaml file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerSettings:
    coordinator_url: str
    worker_token: str
    worker_id: str
    cache_dir: Path
    work_dir: Path
    max_concurrent_jobs: int
    execution_mode: str
    scoring_version: str
    log_level: str
    cache_max_bytes: int
    max_memory_gb: int
    max_timeout_seconds: int

    @classmethod
    def load(cls, path: Path | None = None) -> "WorkerSettings":
        document: dict[str, object] = {}
        if path is not None:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise SettingsError("worker config must be a mapping")
            if "worker_token" in loaded or "token" in loaded:
                raise SettingsError("Put the worker token in WORKER_TOKEN, not the yaml file")
            document = loaded

        def pick(name: str, env: str, default: object = None) -> object:
            if os.environ.get(env) not in (None, ""):
                return os.environ[env]
            if name in document and document[name] not in (None, ""):
                return document[name]
            return default

        mode = str(pick("execution_mode", "EXECUTION_MODE", "official"))
        version = str(pick("scoring_version", "SCORING_VERSION", ""))
        if mode not in {"mock", "official"}:
            raise SettingsError("execution_mode must be mock or official")
        if version not in {"v1", "v2"}:
            raise SettingsError("scoring_version must be v1 or v2")
        token = str(pick("worker_token", "WORKER_TOKEN", "") or "")
        url = str(pick("coordinator_url", "COORDINATOR_URL", "") or "")
        worker_id = str(pick("worker_id", "WORKER_ID", "") or "")
        if not token or not url or not worker_id:
            raise SettingsError("COORDINATOR_URL, WORKER_TOKEN, and WORKER_ID are required")
        cache_dir = Path(str(pick("cache_dir", "CACHE_DIR", "cache"))).expanduser()
        work_dir = Path(str(pick("work_dir", "WORK_DIR", "work"))).expanduser()
        return cls(
            coordinator_url=url.rstrip("/"),
            worker_token=token,
            worker_id=worker_id,
            cache_dir=cache_dir,
            work_dir=work_dir,
            max_concurrent_jobs=int(pick("max_concurrent_jobs", "MAX_CONCURRENT_JOBS", 1)),
            execution_mode=mode,
            scoring_version=version,
            log_level=str(pick("log_level", "LOG_LEVEL", "INFO")),
            cache_max_bytes=int(pick("cache_max_bytes", "CACHE_MAX_BYTES", 100 * 1024**3)),
            max_memory_gb=int(pick("max_memory_gb", "MAX_MEMORY_GB", 64)),
            max_timeout_seconds=int(pick("max_timeout_seconds", "MAX_TIMEOUT_SECONDS", 24 * 3600)),
        )
