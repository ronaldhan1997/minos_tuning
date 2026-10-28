"""Coordinator CLI. Dataset import reads files on the machine where it runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from apps.coordinator.auth import hash_password
from apps.coordinator.errors import ServiceError
from apps.coordinator.service import Coordinator
from database.session import init_engine
from shared.schemas import ExperimentCreate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minosctl")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP coordinator")
    serve.add_argument("--host", default=os.environ.get("COORDINATOR_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("COORDINATOR_PORT", "8080")))

    dataset = sub.add_parser("dataset")
    dataset_sub = dataset.add_subparsers(dest="dataset_command", required=True)
    imported = dataset_sub.add_parser("import")
    imported.add_argument("--manifest", required=True, type=Path)
    practice = dataset_sub.add_parser("import-practice")
    practice.add_argument("--sample", required=True, type=Path)
    practice.add_argument("--scoring-version", required=True, choices=["v1", "v2"])
    dataset_sub.add_parser("list")

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("hash-password", help="print a PBKDF2 hash of DASHBOARD_ADMIN_PASSWORD")

    token = sub.add_parser("worker")
    token_sub = token.add_subparsers(dest="worker_command", required=True)
    issue = token_sub.add_parser("token")
    issue.add_argument("worker_id")

    experiment = sub.add_parser("experiment")
    exp_sub = experiment.add_subparsers(dest="experiment_command", required=True)
    create = exp_sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--search-space", type=Path, default=None)
    create.add_argument("--profile", default="stage1")
    create.add_argument("--trials", type=int, required=True)
    create.add_argument("--scoring-version", required=True, choices=["v1", "v2"])
    create.add_argument("--train", action="append", required=True)
    create.add_argument("--holdout", action="append", default=[])
    create.add_argument("--seed", type=int, default=1)
    create.add_argument("--execution-mode", choices=["mock", "official"], default="official")
    create.add_argument("--variability-penalty", type=float, default=0.0)
    create.add_argument("--top-n", type=int, default=3)
    create.add_argument("--max-parallel", type=int, default=8)
    create.add_argument("--timeout", type=int, default=3600)
    create.add_argument("--threads", type=int, default=1)
    create.add_argument("--memory-gb", type=int, default=4)
    status = exp_sub.add_parser("status")
    status.add_argument("name")
    export = exp_sub.add_parser("export")
    export.add_argument("name")
    export.add_argument("--output", required=True, type=Path)
    for name in ("pause", "resume", "cancel"):
        command = exp_sub.add_parser(name)
        command.add_argument("name")

    args = parser.parse_args(argv)
    try:
        if args.command == "auth":
            password = os.environ.get("DASHBOARD_ADMIN_PASSWORD", "")
            if not password:
                print("error: set DASHBOARD_ADMIN_PASSWORD in the environment", file=sys.stderr)
                return 1
            print(hash_password(password))
            return 0
        if args.command == "serve":
            return _serve(args.host, args.port)
        if args.command == "dataset":
            return _dataset(args)
        if args.command == "worker":
            print(_local().issue_token(args.worker_id))
            return 0
        return _experiment(args)
    except ServiceError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1


def _local() -> Coordinator:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ServiceError(1, "config", "DATABASE_URL is required for coordinator CLI commands")
    init_engine(url)
    return Coordinator(
        os.environ.get("COORDINATOR_ADMIN_TOKEN", "local"),
        int(os.environ.get("LEASE_GRACE_SECONDS", "120")),
    )


def _dataset(args: argparse.Namespace) -> int:
    coord = _local()
    if args.dataset_command == "list":
        print(json.dumps(coord.list_datasets(), indent=2))
        return 0
    if args.dataset_command == "import":
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
        print(json.dumps(coord.import_document(document), indent=2))
        return 0
    print(json.dumps(coord.import_practice(args.sample, args.scoring_version), indent=2))
    return 0


def _experiment(args: argparse.Namespace) -> int:
    if os.environ.get("COORDINATOR_URL") and args.experiment_command != "create":
        return _remote_experiment(args)
    coord = _local()
    if args.experiment_command == "create":
        document = None
        if args.search_space is not None:
            document = yaml.safe_load(args.search_space.read_text(encoding="utf-8"))
        body = ExperimentCreate(
            name=args.name,
            search_profile=args.profile,
            search_space=document,
            trials=args.trials,
            seed=args.seed,
            scoring_version=args.scoring_version,
            training_dataset_ids=args.train,
            held_out_dataset_ids=args.holdout,
            variability_penalty=args.variability_penalty,
            execution_mode=args.execution_mode,
            top_n_validate=args.top_n,
            max_parallel_global=args.max_parallel,
            job_timeout_seconds=args.timeout,
            gatk_threads=args.threads,
            memory_gb=args.memory_gb,
        )
        if os.environ.get("COORDINATOR_URL"):
            return _post_create(body)
        print(json.dumps(coord.create_experiment(body), indent=2))
        return 0
    if args.experiment_command == "status":
        print(json.dumps(coord.get_experiment(args.name), indent=2))
        return 0
    if args.experiment_command == "export":
        text = coord.export_conf(args.name)
        view = coord.get_experiment(args.name)
        _write_export(args.output, text, view["selection"])
        print(args.output)
        return 0
    action = {"pause": coord.pause, "resume": coord.resume, "cancel": coord.cancel}[args.experiment_command]
    print(json.dumps(action(args.name), indent=2))
    return 0


def _post_create(body: ExperimentCreate) -> int:
    import httpx

    response = httpx.post(
        os.environ["COORDINATOR_URL"].rstrip("/") + "/api/v1/experiments",
        json=body.model_dump(),
        headers=_admin_headers(),
        timeout=60.0,
    )
    if response.status_code >= 400:
        print(response.text, file=sys.stderr)
        return 1
    print(response.text)
    return 0


def _remote_experiment(args: argparse.Namespace) -> int:
    import httpx

    base = os.environ["COORDINATOR_URL"].rstrip("/")
    headers = _admin_headers()
    if args.experiment_command == "export":
        response = httpx.get(f"{base}/api/v1/experiments/{args.name}/export", headers=headers, timeout=60.0)
        view = httpx.get(f"{base}/api/v1/experiments/{args.name}", headers=headers, timeout=60.0)
        if response.status_code >= 400 or view.status_code >= 400:
            print(response.text or view.text, file=sys.stderr)
            return 1
        _write_export(args.output, response.text, view.json().get("selection"))
        print(args.output)
        return 0
    path = {
        "status": f"/api/v1/experiments/{args.name}",
        "pause": f"/api/v1/experiments/{args.name}/pause",
        "resume": f"/api/v1/experiments/{args.name}/resume",
        "cancel": f"/api/v1/experiments/{args.name}/cancel",
    }[args.experiment_command]
    method = "GET" if args.experiment_command == "status" else "POST"
    response = httpx.request(method, base + path, headers=headers, timeout=60.0)
    if response.status_code >= 400:
        print(response.text, file=sys.stderr)
        return 1
    print(response.text)
    return 0


def _admin_headers() -> dict[str, str]:
    token = os.environ.get("COORDINATOR_ADMIN_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _write_export(path: Path, text: str, selection: object) -> None:
    from shared.atomic import atomic_write_json, atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    sidecar = path.with_suffix(".provenance.json")
    atomic_write_json(sidecar, selection)


def _serve(host: str, port: int) -> int:
    import uvicorn

    if host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "WARNING: the coordinator is listening beyond loopback. "
            "Keep it on the trusted LAN, do not forward the port, and put TLS in front of an untrusted LAN.",
            file=sys.stderr,
        )
    print(
        "WARNING: this process speaks HTTP. Tokens travel in clear text. "
        "Use a reverse proxy with TLS when the LAN is not fully trusted.",
        file=sys.stderr,
    )
    uvicorn.run("apps.coordinator.app:app_from_env", factory=True, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
