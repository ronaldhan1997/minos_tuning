"""Read-only projections. Missing scores stay null; zero stays zero."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

from apps.coordinator.clock import iso, parse_iso, utc_now
from apps.coordinator.errors import ServiceError

HEARTBEAT_STALE_SECONDS = 45
_HIDDEN_KEYS = {
    "local_path",
    "path",
    "bam",
    "work_dir",
    "cache_dir",
    "stdout_path",
    "stderr_path",
}


def encode_cursor(created_at: str, row_id: str) -> str:
    raw = f"{created_at}\n{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        pad = "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(cursor + pad).decode("utf-8")
        created_at, row_id = text.split("\n", 1)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ServiceError(400, "cursor", "cursor is invalid") from exc
    if not created_at or not row_id:
        raise ServiceError(400, "cursor", "cursor is invalid")
    return created_at, row_id


def heartbeat_age_seconds(last_heartbeat: str | None, now: datetime | None = None) -> float | None:
    if not last_heartbeat:
        return None
    moment = now or utc_now()
    return (moment - parse_iso(last_heartbeat)).total_seconds()


def presence_status(
    *,
    last_heartbeat: str | None,
    execution_mode: str,
    docker_ready: bool,
    minos_commit: str,
    pinned_commit: str,
    scheduling_mode: str,
    active_jobs: int,
    now: datetime | None = None,
    stale_after: int = HEARTBEAT_STALE_SECONDS,
) -> str:
    age = heartbeat_age_seconds(last_heartbeat, now)
    if age is None:
        return "unknown"
    if age > stale_after:
        return "offline"
    if execution_mode == "official" and not docker_ready:
        return "incompatible"
    if minos_commit != pinned_commit:
        return "incompatible"
    if scheduling_mode == "draining":
        return "draining"
    if scheduling_mode == "paused":
        return "paused"
    if active_jobs > 0:
        return "online-busy"
    return "online-idle"


def runtime_note(platform: str, host_os: str) -> str:
    text = f"{platform} {host_os}".lower()
    if "win" in text:
        return (
            "Windows host agent. Linux containers require Docker Desktop with WSL2. "
            "The agent does not say which of those is running."
        )
    return "Linux host agent using Docker Engine."


def public_mapping(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _HIDDEN_KEYS or key.endswith("_path"):
            continue
        if isinstance(value, str) and (value.startswith("/") or ":\\" in value or value.startswith("\\\\")):
            continue
        cleaned[key] = value
    return cleaned


def best_so_far(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Completion order. Trials without an objective are omitted, not plotted as zero."""
    scored = [trial for trial in trials if trial.get("objective") is not None and trial.get("told")]
    scored.sort(key=lambda trial: (str(trial.get("updated_at") or ""), int(trial.get("optuna_number") or 0)))
    best: float | None = None
    series: list[dict[str, Any]] = []
    for trial in scored:
        value = float(trial["objective"])
        best = value if best is None else max(best, value)
        series.append(
            {
                "trial_id": trial["trial_id"],
                "optuna_number": trial["optuna_number"],
                "completed_at": trial.get("updated_at"),
                "objective": value,
                "best_so_far": best,
                "score_source": trial.get("score_source"),
                "is_baseline": bool(trial.get("is_baseline")),
            }
        )
    return series


def eta_seconds(elapsed: list[float], remaining_jobs: int) -> dict[str, Any] | None:
    if remaining_jobs <= 0 or len(elapsed) < 3:
        return None
    mean = sum(elapsed) / len(elapsed)
    return {
        "label": "estimate",
        "remaining_jobs": remaining_jobs,
        "seconds": int(mean * remaining_jobs),
        "based_on_completed_jobs": len(elapsed),
        "note": "Estimated from completed job durations. It is not a score forecast.",
    }


def hour_cutoff(now: datetime | None = None) -> str:
    return iso((now or utc_now()) - timedelta(hours=1))
