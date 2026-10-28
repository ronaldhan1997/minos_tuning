"""How much of a computer one GATK job may use.

One core and 2 GB stay free so the operating system, Docker, and the client
keep running. The job also stays inside memory that is free right now.
"""

from __future__ import annotations

_RESERVED_CORES = 1
_RESERVED_MB = 2048
_WORKER_MEMORY_CAP_GB = 64


def usable_resources(*, cpu_count: int, ram_mb: int, ram_used_mb: int | None) -> tuple[int, int]:
    threads = max(1, int(cpu_count) - _RESERVED_CORES)
    from_total = max(1024, int(ram_mb) - _RESERVED_MB)
    if ram_used_mb is None:
        budget_mb = from_total
    else:
        free_mb = max(0, int(ram_mb) - int(ram_used_mb))
        budget_mb = min(from_total, max(1024, free_mb - _RESERVED_MB))
    memory_gb = max(1, min(_WORKER_MEMORY_CAP_GB, budget_mb // 1024))
    return threads, memory_gb
