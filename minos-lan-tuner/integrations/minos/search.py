"""Search space checked against ``GATK_QUALITY_PARAMS``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from integrations.minos.pin import ensure_import_paths


class SearchSpaceError(ValueError):
    pass


@dataclass(frozen=True)
class SearchSpace:
    profile: str
    parameters: dict[str, dict[str, Any]]

    def names(self) -> list[str]:
        return list(self.parameters)


def quality_params() -> dict[str, Any]:
    ensure_import_paths()
    from templates.tool_params import GATK_QUALITY_PARAMS

    return GATK_QUALITY_PARAMS


def default_search_path() -> Path:
    from integrations.minos.pin import tuner_root

    return tuner_root() / "config" / "search_space.yaml"


def space_from_document(document: dict[str, Any], profile: str) -> SearchSpace:
    if not isinstance(document, dict) or profile not in document:
        raise SearchSpaceError(f"Search profile {profile!r} is not in the document")
    block = document[profile]
    raw = block.get("parameters") if isinstance(block, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise SearchSpaceError(f"Profile {profile} has no parameters")
    return parse_search_parameters(raw, profile=profile)


def load_search_space(path: Path, profile: str) -> SearchSpace:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SearchSpaceError(f"Search space file is not a mapping: {path}")
    try:
        return space_from_document(document, profile)
    except SearchSpaceError as exc:
        raise SearchSpaceError(f"{exc} ({path})") from exc


def parse_search_parameters(raw: dict[str, Any], *, profile: str) -> SearchSpace:
    official = quality_params()
    checked: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if name == "emit_ref_confidence":
            raise SearchSpaceError(
                "emit_ref_confidence is fixed to NONE. "
                "GVCF and BP_RESOLUTION change the VCF shape hap.py scores."
            )
        if name not in official:
            raise SearchSpaceError(
                f"Parameter {name!r} is not in templates/tool_params.py GATK_QUALITY_PARAMS"
            )
        if not isinstance(spec, dict):
            raise SearchSpaceError(f"Parameter {name} must be a mapping")
        definition = official[name]
        expected = definition["type"]
        if spec.get("type") != expected:
            raise SearchSpaceError(
                f"Parameter {name} type {spec.get('type')!r} does not match official type {expected}"
            )
        if expected in {"int", "float"}:
            if "low" not in spec or "high" not in spec:
                raise SearchSpaceError(f"Parameter {name} needs low and high")
            if float(spec["low"]) != float(definition["min"]) or float(spec["high"]) != float(definition["max"]):
                raise SearchSpaceError(
                    f"Parameter {name} bounds {spec['low']}..{spec['high']} "
                    f"differ from official {definition['min']}..{definition['max']}"
                )
            if spec.get("log") and float(spec["low"]) <= 0:
                raise SearchSpaceError(f"Parameter {name} cannot use a log sampler with low <= 0")
        elif expected == "enum":
            choices = list(spec.get("choices") or [])
            allowed = list(definition["allowed_values"])
            if choices != allowed:
                raise SearchSpaceError(
                    f"Parameter {name} choices {choices} differ from official {allowed}"
                )
        elif expected == "bool":
            extra = set(spec) - {"type"}
            if extra:
                raise SearchSpaceError(f"Boolean parameter {name} only accepts a type field, got {sorted(extra)}")
        else:
            raise SearchSpaceError(f"Unsupported official type {expected} for {name}")
        checked[name] = dict(spec)
    return SearchSpace(profile=profile, parameters=checked)


def baseline_params(space: SearchSpace) -> dict[str, Any]:
    official = quality_params()
    return {name: official[name]["default"] for name in space.parameters}


def full_config(overrides: dict[str, Any]) -> dict[str, Any]:
    """Every official quality key, defaults first, then verified overrides.

    ``emit_ref_confidence`` stays NONE. Passing the full mapping matches
    ``configs/gatk.conf``: the template receives explicit values for every
    whitelisted key.
    """
    if overrides.get("emit_ref_confidence", "NONE") != "NONE":
        raise SearchSpaceError("emit_ref_confidence cannot be changed from NONE")
    unknown = set(overrides) - set(quality_params())
    if unknown:
        raise SearchSpaceError(f"Unknown GATK parameters: {sorted(unknown)}")
    config = {name: spec["default"] for name, spec in quality_params().items()}
    config.update(overrides)
    config["emit_ref_confidence"] = "NONE"
    ensure_import_paths()
    from templates.tool_params import validate_and_build_flags

    result = validate_and_build_flags("gatk", config)
    if not result["valid"]:
        raise SearchSpaceError("Official validator rejected the config: " + "; ".join(result["errors"]))
    return config


def suggest(trial: Any, space: SearchSpace) -> dict[str, Any]:
    suggested: dict[str, Any] = {}
    for name, spec in space.parameters.items():
        kind = spec["type"]
        if kind == "int":
            suggested[name] = trial.suggest_int(name, int(spec["low"]), int(spec["high"]))
        elif kind == "float":
            suggested[name] = trial.suggest_float(
                name,
                float(spec["low"]),
                float(spec["high"]),
                log=bool(spec.get("log", False)),
            )
        elif kind == "enum":
            suggested[name] = trial.suggest_categorical(name, list(spec["choices"]))
        elif kind == "bool":
            suggested[name] = bool(trial.suggest_categorical(name, [False, True]))
        else:
            raise SearchSpaceError(f"Cannot suggest {name}")
    return suggested


def dump_conf(options: dict[str, Any], header_lines: list[str]) -> str:
    """``key=value`` text using the same literals ``utils.config_loader`` parses."""
    lines = [f"# {line}" for line in header_lines]
    lines.append("# Generated by minos-lan-tuner. Quality keys only.")
    for key in sorted(options):
        value = options[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, float):
            rendered = format(value, ".16g")
        else:
            rendered = str(value)
        lines.append(f"{key}={rendered}")
    return "\n".join(lines) + "\n"


def parse_conf_text(text: str) -> dict[str, Any]:
    ensure_import_paths()
    from utils.config_loader import _parse_value

    options: dict[str, Any] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SearchSpaceError(f"line {line_number}: missing '='")
        key, _, value = line.partition("=")
        key = key.strip()
        if key in options:
            raise SearchSpaceError(f"line {line_number}: duplicate key {key}")
        options[key] = _parse_value(value.strip())
    return options
