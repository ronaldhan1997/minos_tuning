"""Server-rendered status page. It does not show filesystem paths or tokens."""

from __future__ import annotations

from html import escape
from typing import Any


def render_dashboard(snapshot: dict[str, Any]) -> str:
    workers = snapshot.get("workers") or []
    experiments = snapshot.get("experiments") or []
    failures = snapshot.get("failures") or []
    parts = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        "<title>Minos LAN tuner</title>",
        "<style>",
        "body{font-family:sans-serif;margin:2rem;color:#1f2933;background:#f7f6f3}",
        "table{border-collapse:collapse;width:100%;background:white;margin:1rem 0}",
        "th,td{border-bottom:1px solid #e4e2dc;text-align:left;padding:.4rem .6rem;vertical-align:top}",
        "h1,h2{font-weight:600} .warn{color:#8a4b08}",
        "</style></head><body>",
        "<h1>Minos LAN tuner</h1>",
        "<p class='warn'>LAN dashboard. Do not publish this port. Scores labeled mock are not AdvancedScorer results.</p>",
        "<h2>Workers</h2>",
        _table(
            ["worker", "os", "platform", "cpu", "ram MB", "free disk MB", "parallel", "docker", "scorer", "commit", "heartbeat"],
            [
                [
                    worker["worker_id"],
                    worker["host_os"],
                    worker["platform"],
                    worker["cpu_count"],
                    worker["ram_mb"],
                    worker["free_disk_mb"],
                    worker["max_parallel_jobs"],
                    worker["docker_ready"],
                    f"{worker['scorer_version']} {worker['scorer_name']}",
                    str(worker["minos_commit"])[:12],
                    worker.get("last_heartbeat") or "",
                ]
                for worker in workers
            ],
        ),
        "<h2>Experiments</h2>",
    ]
    if not experiments:
        parts.append("<p>No experiments yet.</p>")
    for exp in experiments:
        source = exp.get("best_score_source") or exp["execution_mode"]
        parts.append(f"<h3>{escape(str(exp['name']))}</h3>")
        parts.append(
            "<p>"
            f"status {escape(str(exp['status']))} · "
            f"mode {escape(str(exp['execution_mode']))} · "
            f"score source {escape(str(source))} · "
            f"scoring {escape(str(exp['scoring_version']))} ({escape(str(exp['scorer_name']))}) · "
            f"commit {escape(str(exp['minos_commit'])[:12])} · "
            f"trials {exp['trials_told']}/{exp['budget']}"
            "</p>"
        )
        parts.append(f"<p>Datasets {escape(str(exp['training_dataset_ids']))}. Held-out {escape(str(exp['held_out_dataset_ids']))}.</p>")
        parts.append(_sparkline(exp.get("best_trend") or []))
        if exp.get("best_objective") is not None:
            parts.append(f"<p>Best training objective {escape(str(exp['best_objective']))} ({escape(str(source))}).</p>")
        if exp["status"] == "COMPLETED":
            parts.append(
                f"<p>Export: <a href='/api/v1/experiments/{escape(str(exp['id']))}/export'>gatk.conf</a> "
                "(loopback is open when MINOS_ALLOW_LOOPBACK_ADMIN=1; other clients send the admin token).</p>"
            )
    parts.append("<h2>Recent job errors</h2>")
    parts.append(
        _table(
            ["job", "dataset", "state", "class", "message"],
            [
                [row["job_id"], row["dataset_id"], row["state"], row["error_class"], row["message"]]
                for row in failures
            ],
        )
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def _sparkline(values: list[float]) -> str:
    if not values:
        return "<p>No completed-trial trend yet.</p>"
    width, height = 480, 80
    low = min(values)
    high = max(values)
    span = high - low or 1.0
    points = []
    for index, value in enumerate(values):
        x = 2 if len(values) == 1 else 2 + index * (width - 4) / (len(values) - 1)
        y = height - 4 - (value - low) / span * (height - 8)
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' role='img'>"
        f"<polyline fill='none' stroke='#1f6feb' stroke-width='2' points='{' '.join(points)}'/>"
        "</svg>"
        f"<p>Best-so-far training objective from {escape(str(low))} to {escape(str(high))}.</p>"
    )


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{escape(str(item))}</th>" for item in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(item))}</td>" for item in row)
        body.append(f"<tr>{cells}</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(headers)}'>none</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
