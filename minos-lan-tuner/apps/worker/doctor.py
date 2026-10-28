"""Local readiness checks. A passing doctor run is not a Windows end-to-end test."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

from apps.worker.settings import WorkerSettings
from integrations.minos.pin import (
    bcftools_image,
    commit_matches_pin,
    docker_executable,
    gatk_image,
    happy_image,
    head_commit,
    inspect_image_id,
    run_docker,
)


def run_doctor(settings: WorkerSettings | None, *, probe_coordinator: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "python",
            "ok": sys.version_info >= (3, 11),
            "required": True,
            "detail": sys.version.split()[0],
        }
    )
    checks.append(
        {
            "name": "minos_commit",
            "ok": commit_matches_pin(),
            "required": True,
            "detail": head_commit(),
        }
    )
    docker_ok, docker_detail = _docker()
    checks.append({"name": "docker", "ok": docker_ok, "required": _official(settings), "detail": docker_detail})
    for name, image in (
        ("gatk_image", gatk_image()),
        ("happy_image", happy_image()),
        ("bcftools_image", bcftools_image()),
    ):
        image_id = inspect_image_id(image) if docker_ok else None
        checks.append(
            {
                "name": name,
                "ok": image_id is not None,
                "required": _official(settings),
                "detail": image_id or f"{image} is not present locally",
            }
        )
    if settings is not None:
        for label, directory in (("cache_dir", settings.cache_dir), ("work_dir", settings.work_dir)):
            checks.append({"name": label, "ok": _writable(directory), "required": True, "detail": str(directory)})
        if probe_coordinator and settings.coordinator_url:
            checks.append(_coordinator(settings))
    checks.append(_windows_note())
    return {
        "ok": all(item["ok"] or not item["required"] for item in checks),
        "platform": sys.platform,
        "checks": checks,
    }


def _official(settings: WorkerSettings | None) -> bool:
    return settings is None or settings.execution_mode == "official"


def _docker() -> tuple[bool, str]:
    if docker_executable() is None:
        return False, "Docker Desktop was not found. Install it and leave it running."
    result = run_docker(["version", "--format", "{{.Server.Version}}"], timeout=20, capture_stdout=True)
    if result is None:
        return False, "Docker did not answer."
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker version failed").strip()
        return False, detail[:300]
    return True, f"server {(result.stdout or '').strip()}"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _coordinator(settings: WorkerSettings) -> dict[str, Any]:
    try:
        response = httpx.get(settings.coordinator_url + "/healthz", timeout=5.0)
        ok = response.status_code == 200
        detail = f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        ok = False
        detail = str(exc)
    return {"name": "coordinator", "ok": ok, "required": False, "detail": detail}


def _windows_note() -> dict[str, Any]:
    if sys.platform == "win32":
        detail = (
            "This process is Windows. Linux GATK and hap.py run only inside Docker "
            "Desktop's Linux containers (WSL2). This build does not record a completed "
            "Windows hardware run. Confirm `docker version` shows a Linux engine, the "
            "drive is shared, and paths with spaces survive `minos-worker doctor`."
        )
    else:
        detail = (
            "This host is not Windows. Windows path conversion is covered by unit tests. "
            "A Windows 11 Docker Desktop run has not been executed here."
        )
    return {"name": "windows_hardware", "ok": False, "required": False, "detail": detail}


_cpu_sample: tuple[int, int] | None = None


def host_usage(path: Path) -> dict[str, int | float | None]:
    """Current CPU percent, memory in use, and disk size for this computer."""
    path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(path)
    ram_total, ram_used = _ram_pair()
    return {
        "cpu_percent": _cpu_percent(),
        "ram_used_mb": ram_used,
        "free_disk_mb": int(disk.free / (1024 * 1024)),
        "disk_total_mb": int(disk.total / (1024 * 1024)),
        "ram_total_mb": ram_total,
    }


def disk_free_mb(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return int(shutil.disk_usage(path).free / (1024 * 1024))


def ram_mb() -> int:
    total, _used = _ram_pair()
    return total


def _ram_pair() -> tuple[int, int]:
    if sys.platform == "win32":
        return _windows_ram_pair()
    total = 0
    available = 0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        total = int(pages * size / (1024 * 1024))
    except (AttributeError, ValueError, OSError):
        total = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError, IndexError):
        available = 0
    used = max(0, total - available) if total else 0
    return total, used


def _cpu_percent() -> float | None:
    global _cpu_sample
    if sys.platform == "win32":
        times = _windows_cpu_times()
    else:
        times = _linux_cpu_times()
    if times is None:
        return None
    previous = _cpu_sample
    _cpu_sample = times
    if previous is None:
        return None
    idle_delta = times[0] - previous[0]
    total_delta = times[1] - previous[1]
    if total_delta <= 0:
        return 0.0
    used = 100.0 * (1.0 - (idle_delta / total_delta))
    return max(0.0, min(100.0, used))


def _linux_cpu_times() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(item) for item in first]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _windows_ram_pair() -> tuple[int, int]:
    status = _windows_memory_status()
    if status is None:
        return 0, 0
    total = int(status.ullTotalPhys / (1024 * 1024))
    available = int(status.ullAvailPhys / (1024 * 1024))
    return total, max(0, total - available)


def _windows_memory_status() -> Any:
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return status
    except (AttributeError, OSError, ValueError):
        return None


def _windows_cpu_times() -> tuple[int, int] | None:
    try:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        idle, kernel, user = FileTime(), FileTime(), FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def ticks(value: FileTime) -> int:
            return (int(value.high) << 32) + int(value.low)

        idle_ticks = ticks(idle)
        total = ticks(kernel) + ticks(user)
        return idle_ticks, total
    except (AttributeError, OSError, ValueError):
        return None
