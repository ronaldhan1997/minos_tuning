"""Build a dataset manifest from local, already-downloaded practice files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from datasets.layout import REQUIRED_ROLES, cache_relative
from integrations.minos.pin import ensure_import_paths
from shared.hashing import canonical_json, sha256_file, sha256_text

_GRCH37 = re.compile(r"GRCh37|hg19", re.IGNORECASE)


class ManifestError(ValueError):
    pass


def _names_from_files(files: list[dict[str, Any]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in files:
        if item["role"] in {"bam", "reference", "truth", "mutations"}:
            found[item["role"]] = Path(item["path"]).name
    return found


def expand_manifest_files(raw_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand an SDF directory into one record per file and assign cache paths."""
    names = _names_from_files(raw_files)
    expanded: list[dict[str, Any]] = []
    for item in raw_files:
        role = item["role"]
        source = Path(item["path"]).expanduser()
        if role == "sdf":
            if not source.is_dir():
                raise ManifestError(f"SDF path is not a directory: {source}")
            children = [path for path in source.rglob("*") if path.is_file()]
            if not children:
                raise ManifestError(f"SDF directory is empty: {source}")
            for child in sorted(children):
                if child.is_symlink():
                    raise ManifestError(f"SDF symlink is not imported: {child}")
                relative = child.relative_to(source).as_posix()
                expanded.append(
                    {
                        "role": "sdf",
                        "path": str(child.resolve()),
                        "relative_path": cache_relative("sdf", child.name, sdf_relative=relative),
                        "sha256": item.get("sha256") if len(children) == 1 else None,
                        "size_bytes": child.stat().st_size,
                    }
                )
            continue
        if not source.is_file():
            raise ManifestError(f"{role} path is not a file: {source}")
        if source.is_symlink():
            source = source.resolve()
        expanded.append(
            {
                "role": role,
                "path": str(source),
                "relative_path": cache_relative(
                    role,
                    source.name,
                    bam_name=names.get("bam"),
                    reference_name=names.get("reference"),
                    truth_name=names.get("truth"),
                    mutations_name=names.get("mutations"),
                ),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
            }
        )
    return expanded


def hash_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hashed: list[dict[str, Any]] = []
    for item in files:
        digest = sha256_file(item["path"])
        expected = item.get("sha256")
        if expected and expected != digest:
            raise ManifestError(
                f"SHA-256 mismatch for {item['role']} {item['relative_path']}: "
                f"manifest {expected} content {digest}"
            )
        size = Path(item["path"]).stat().st_size
        hashed.append({**item, "sha256": digest, "size_bytes": size})
    return hashed


def reference_header(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        line = handle.readline().strip()
    if _GRCH37.search(line):
        raise ManifestError(f"Reference header looks like GRCh37/hg19: {line}")
    return line


def manifest_sha256(body: dict[str, Any]) -> str:
    public = {
        "dataset_id": body["dataset_id"],
        "chromosome": body.get("chromosome"),
        "files": [
            {
                "relative_path": item["relative_path"],
                "role": item["role"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
            }
            for item in sorted(body["files"], key=lambda item: item["relative_path"])
        ],
        "license_note": body.get("license_note"),
        "reference_build": body["reference_build"],
        "reference_header": body.get("reference_header"),
        "region": body["region"],
        "scoring_version": body["scoring_version"],
        "source": body.get("source"),
    }
    return sha256_text(canonical_json(public))


def validate_manifest(body: dict[str, Any]) -> dict[str, Any]:
    ensure_import_paths()
    from templates.tool_params import validate_region
    from utils.scoring_version import normalise

    dataset_id = str(body.get("dataset_id") or "").strip()
    if not dataset_id:
        raise ManifestError("dataset_id is required")
    region = body.get("region")
    checked = validate_region(region)
    if not checked["valid"]:
        raise ManifestError(checked["error"] or "invalid region")
    version = normalise(body.get("scoring_version"))
    if version is None:
        raise ManifestError("scoring_version must be v1 or v2")
    build = str(body.get("reference_build") or "").strip()
    if not build:
        raise ManifestError("reference_build is required")
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestError("files are required")
    roles = {item["role"] for item in files}
    missing = [role for role in REQUIRED_ROLES if role not in roles]
    if missing:
        raise ManifestError("manifest is missing roles: " + ", ".join(missing))
    reference = next(item for item in files if item["role"] == "reference")
    header = reference_header(reference["path"])
    prepared = dict(body)
    prepared["dataset_id"] = dataset_id
    prepared["region"] = region
    prepared["scoring_version"] = version
    prepared["reference_build"] = build
    prepared["reference_header"] = header
    prepared["files"] = files
    prepared["manifest_sha256"] = manifest_sha256(prepared)
    prepared["reference_sha256"] = reference["sha256"]
    return prepared


def load_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    files = expand_manifest_files(list(raw.get("files") or []))
    files = hash_files(files)
    raw = dict(raw)
    raw["files"] = files
    return validate_manifest(raw)


def from_practice_sample(sample_json: Path, *, scoring_version: str, role_note: str) -> dict[str, Any]:
    """Read a local ``sample.json`` written by the practice downloader.

    The miner passes ``ref_build=GRCh38`` even when the FASTA header is only
    ``>chr19``. This function records that header and still requires the caller
    to pass the scoring version explicitly.
    """
    data = json.loads(sample_json.read_text(encoding="utf-8"))
    mapping = {
        "bam": "bam",
        "bai": "bai",
        "reference": "reference",
        "reference_index": "reference_index",
        "truth": "truth",
        "truth_index": "truth_index",
        "mutations": "mutations",
        "mutations_index": "mutations_index",
        "sdf": "sdf",
    }
    files: list[dict[str, Any]] = []
    stamps = data.get("stamps") or {}
    for key, role in mapping.items():
        file_path = data.get(key)
        if not file_path:
            raise ManifestError(f"sample.json has no {key}")
        entry: dict[str, Any] = {"role": role, "path": file_path}
        stamp = stamps.get(key) or {}
        if role != "sdf" and stamp.get("sha256"):
            entry["sha256"] = stamp["sha256"]
        files.append(entry)
    reference = Path(data["reference"])
    # ``chr19.fa`` -> ``chr19.dict`` via with_suffix. Some trees use ``chr19.fa.dict``.
    plain = reference.with_suffix(".dict")
    sibling = reference.parent / f"{reference.name}.dict"
    for candidate in (plain, sibling):
        if candidate.is_file():
            files.append({"role": "reference_dict", "path": str(candidate)})
            break
    body = {
        "dataset_id": data["sample_id"],
        "name": data.get("sample_id"),
        "region": data["region"],
        "chromosome": data.get("chromosome"),
        "reference_build": "GRCh38",
        "scoring_version": scoring_version,
        "source": role_note,
        "license_note": (
            "Authorized local Minos practice files. The tuner serves them on the "
            "LAN and does not upload them."
        ),
        "files": files,
    }
    return load_manifest_document(body)


def load_manifest_document(raw: dict[str, Any]) -> dict[str, Any]:
    files = expand_manifest_files(list(raw.get("files") or []))
    files = hash_files(files)
    prepared = dict(raw)
    prepared["files"] = files
    return validate_manifest(prepared)
