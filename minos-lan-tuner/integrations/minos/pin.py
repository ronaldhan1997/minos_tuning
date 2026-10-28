"""Locate the pinned Minos checkout and import its packages."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

# Inspected git HEAD of https://github.com/minos-protocol/minos_subnet.
PINNED_COMMIT = "4b3c0944b517d09be9b54eb8fa3158ae700b5d5f"
SOURCE_PATHS = ("templates", "utils", "neurons", "base", "configs")


def tuner_root() -> Path:
    return Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """Minos checkout that contains ``templates/`` and ``start-miner.sh``.

    The tuner may live in a subdirectory such as ``tuning/``. Walk parents
    until that checkout is found. ``MINOS_REPO_ROOT`` still overrides it.
    """
    override = os.environ.get("MINOS_REPO_ROOT")
    if override:
        return Path(override).resolve()
    start = tuner_root()
    for candidate in (start, *start.parents):
        if (candidate / "start-miner.sh").is_file() and (candidate / "templates").is_dir():
            return candidate
    return start.parent


def ensure_import_paths() -> Path:
    """Put this project first and the Minos checkout last on ``sys.path``.

    The checkout contains a ``datasets/`` data directory. This project also has
    a ``datasets`` package. The project path stays in front so ``import datasets``
    loads the package, while ``templates`` and ``utils`` still resolve to Minos.
    """
    root = repo_root()
    project = str(tuner_root())
    repo = str(root)
    if project in sys.path:
        sys.path.remove(project)
    sys.path.insert(0, project)
    if repo not in sys.path:
        sys.path.append(repo)
    return root


def head_commit(root: Path | None = None) -> str:
    if getattr(sys, "frozen", False):
        stamped = os.environ.get("MINOS_COMMIT", "").strip()
        if not stamped:
            raise RuntimeError("This client build has no Minos commit stamp")
        return stamped
    checkout = root or repo_root()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Cannot read git HEAD in {checkout}") from exc


def source_dirty(root: Path | None = None) -> bool:
    """True when the verified source paths differ from HEAD.

    Untracked practice BAMs and local tuning runs do not count. Those files
    are not the scoring implementation.
    """
    checkout = root or repo_root()
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain", "--", *SOURCE_PATHS],
            cwd=checkout,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Cannot read git status in {checkout}") from exc
    return bool(output.strip())


def commit_matches_pin() -> bool:
    return head_commit() == PINNED_COMMIT


def require_pin() -> None:
    if commit_matches_pin():
        return
    if os.environ.get("MINOS_ALLOW_COMMIT_MISMATCH") == "1":
        return
    raise RuntimeError(
        f"Minos HEAD {head_commit()} does not match pinned commit {PINNED_COMMIT}. "
        "Official execution is refused. Set MINOS_ALLOW_COMMIT_MISMATCH=1 only when "
        "you have re-verified the adapter against the new commit."
    )


@lru_cache(maxsize=1)
def gatk_image() -> str:
    ensure_import_paths()
    import templates.gatk as gatk

    text = _module_source(gatk, "templates/gatk.py")
    images = re.findall(r'"(broadinstitute/gatk:[^"]+)"', text)
    unique = list(dict.fromkeys(images))
    if len(unique) != 1:
        raise RuntimeError(f"GATK image literal in templates/gatk.py is ambiguous: {images}")
    return unique[0]


@lru_cache(maxsize=1)
def happy_image() -> str:
    ensure_import_paths()
    from utils.scoring import HAPPY_DOCKER_IMAGE

    return str(HAPPY_DOCKER_IMAGE)


@lru_cache(maxsize=1)
def bcftools_image() -> str:
    ensure_import_paths()
    from utils.scoring import BCFTOOLS_DOCKER_IMAGE

    return str(BCFTOOLS_DOCKER_IMAGE)


def _module_source(module: object, bundle_name: str) -> str:
    """Read a module's .py text. A frozen client ships the file beside the exe extract."""
    path = Path(str(getattr(module, "__file__", "")))
    if path.suffix == ".py" and path.is_file():
        return path.read_text(encoding="utf-8")
    bundled = Path(getattr(sys, "_MEIPASS", "")) / bundle_name
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    raise RuntimeError(f"Cannot read {bundle_name}")


def scorer_name_for(version: str) -> str:
    ensure_import_paths()
    from utils.scoring_version import scorer_name

    return scorer_name(version)


def docker_executable() -> str | None:
    """Find the Docker client. A Windows window often does not inherit the Desktop PATH."""
    found = shutil.which("docker")
    if found:
        return found
    if sys.platform != "win32":
        return None
    for candidate in _windows_docker_candidates():
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_docker_candidates() -> list[Path]:
    names = (
        Path("Docker") / "Docker" / "resources" / "bin" / "docker.exe",
        Path("Docker") / "Docker" / "resources" / "docker.exe",
    )
    roots = [
        os.environ.get(key)
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LocalAppData")
    ]
    return [Path(root) / name for root in roots if root for name in names]


def run_docker(args: list[str], *, timeout: float, capture_stdout: bool = False) -> subprocess.CompletedProcess[str] | None:
    """Run the Docker client. A windowed Windows app has no console, so the window flag is required."""
    exe = docker_executable()
    if exe is None:
        return None
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000
    try:
        return subprocess.run([exe, *args], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


def inspect_image_id(image: str) -> str | None:
    """Return the local Docker image id, or None when the daemon cannot answer."""
    result = run_docker(["image", "inspect", "--format", "{{.Id}}", image], timeout=20, capture_stdout=True)
    if result is None or result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None
