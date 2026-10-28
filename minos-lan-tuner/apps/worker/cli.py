"""Worker CLI. The same module runs on Ubuntu and Windows."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from apps.worker.doctor import run_doctor
from apps.worker.runner import make_client, register, request_stop, serve
from apps.worker.settings import SettingsError, WorkerSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minos-worker")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "start", "register", "status", "stop"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        settings = _settings(args.command, args.config)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if settings is not None:
        logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    if args.command == "doctor":
        report = run_doctor(settings, probe_coordinator=settings is not None)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if settings is None:
        print("error: worker config is required", file=sys.stderr)
        return 1
    if args.command == "status":
        path = settings.work_dir / "worker_status.json"
        print(path.read_text(encoding="utf-8") if path.exists() else "{}")
        return 0
    if args.command == "stop":
        request_stop(settings.work_dir)
        return 0
    with make_client(settings) as client:
        if args.command == "register":
            register(client, settings)
            print(settings.worker_id)
            return 0
        serve(settings, client)
    return 0


def _settings(command: str, path: Path | None) -> WorkerSettings | None:
    if command == "doctor" and path is None and not _env_ready():
        return None
    return WorkerSettings.load(path)


def _env_ready() -> bool:
    import os

    return all(os.environ.get(name) for name in ("COORDINATOR_URL", "WORKER_TOKEN", "WORKER_ID", "SCORING_VERSION"))


if __name__ == "__main__":
    raise SystemExit(main())
