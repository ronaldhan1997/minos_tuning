"""Stable hashes for configs, manifests, and evaluation cache keys."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Serialize JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def config_sha256(options: dict[str, Any]) -> str:
    return sha256_text(canonical_json(options))


def evaluation_cache_key(
    *,
    config_sha256: str,
    manifest_sha256: str,
    reference_sha256: str,
    minos_commit: str,
    scoring_version: str,
    gatk_image: str,
    gatk_image_id: str,
    happy_image: str,
    bcftools_image: str,
    threads: int,
    memory_gb: int,
    execution_mode: str,
) -> str:
    """Identity of one evaluation.

    A change to any field is a cache miss. Mock and official runs never share
    a key. The mock image id is the literal ``mock`` because a mock run does
    not execute a container digest.
    """
    image_id = "mock" if execution_mode == "mock" else gatk_image_id
    payload = {
        "bcftools_image": bcftools_image,
        "config_sha256": config_sha256,
        "execution_mode": execution_mode,
        "gatk_image": gatk_image,
        "gatk_image_id": image_id,
        "happy_image": happy_image,
        "manifest_sha256": manifest_sha256,
        "memory_gb": memory_gb,
        "minos_commit": minos_commit,
        "reference_sha256": reference_sha256,
        "scoring_version": scoring_version,
        "threads": threads,
    }
    return sha256_text(canonical_json(payload))
