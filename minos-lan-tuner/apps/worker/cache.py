"""Content-addressed dataset cache. Downloads resume from a partial file."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx

from shared.atomic import atomic_write_json
from shared.hashing import sha256_file


class CacheError(RuntimeError):
    pass


def dataset_root(cache_dir: Path, dataset_id: str) -> Path:
    return cache_dir / dataset_id


def cached_dataset_ids(cache_dir: Path) -> list[str]:
    if not cache_dir.exists():
        return []
    found = []
    for child in sorted(cache_dir.iterdir()):
        if child.is_dir() and (child / "dataset_manifest.json").is_file():
            found.append(child.name)
    return found


def ensure_dataset(
    client: httpx.Client,
    cache_dir: Path,
    dataset_id: str,
    *,
    cache_max_bytes: int,
) -> tuple[Path, dict[str, Any]]:
    root = dataset_root(cache_dir, dataset_id)
    root.mkdir(parents=True, exist_ok=True)
    response = client.get(f"/api/v1/datasets/{dataset_id}/manifest")
    if response.status_code != 200:
        raise CacheError(f"manifest request failed with HTTP {response.status_code}")
    manifest = response.json()
    for item in manifest["files"]:
        target = root / item["relative_path"]
        if target.is_file() and sha256_file(target) == item["sha256"] and target.stat().st_size == item["size_bytes"]:
            continue
        _download(client, dataset_id, item["sha256"], target, item["sha256"], int(item["size_bytes"]))
    atomic_write_json(root / "dataset_manifest.json", manifest)
    _enforce_limit(cache_dir, cache_max_bytes, keep={dataset_id})
    return root, manifest


def role_paths(root: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for item in manifest["files"]:
        if item["role"] == "sdf":
            found["sdf"] = root / "sdf"
        else:
            found[item["role"]] = root / item["relative_path"]
    required = ("bam", "bai", "reference", "reference_index", "truth", "truth_index", "mutations", "sdf")
    missing = [role for role in required if role not in found]
    if missing:
        raise CacheError("dataset cache is missing roles: " + ", ".join(missing))
    return found


def _download(
    client: httpx.Client,
    dataset_id: str,
    sha256: str,
    target: Path,
    expected: str,
    size: int,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    existing = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    url = f"/api/v1/datasets/{dataset_id}/blobs/{sha256}"
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 200:
            mode = "wb"
        elif response.status_code == 206 and existing:
            mode = "ab"
        else:
            raise CacheError(f"download of {target.name} failed with HTTP {response.status_code}")
        with partial.open(mode) as handle:
            for block in response.iter_bytes():
                handle.write(block)
    digest = sha256_file(partial)
    if digest != expected or partial.stat().st_size != size:
        partial.unlink(missing_ok=True)
        raise CacheError(f"SHA-256 or size mismatch for {target.name}")
    partial.replace(target)


def _enforce_limit(cache_dir: Path, max_bytes: int, keep: set[str]) -> None:
    datasets = [path for path in cache_dir.iterdir() if path.is_dir()] if cache_dir.exists() else []
    sizes = {path: _dir_size(path) for path in datasets}
    total = sum(sizes.values())
    for path in sorted(datasets, key=lambda item: item.stat().st_mtime):
        if total <= max_bytes:
            return
        if path.name in keep:
            continue
        total -= sizes[path]
        shutil.rmtree(path)


def _dir_size(path: Path) -> int:
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
