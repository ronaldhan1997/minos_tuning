"""Tail the GATK container for a job running on this computer.

The worker also uploads a log on its heartbeat. This reader covers the worker
that is already running here, before that upload exists. A container is used
only when its mount path contains the job id, so another program's GATK log
is left alone.
"""

from __future__ import annotations

import re

from integrations.minos.pin import run_docker

_NAME = re.compile(r"minos-[A-Za-z0-9_-]{1,80}")


def local_container_log(job_id: str, *, tail: int = 80) -> str | None:
    if not job_id or "/" in job_id or ".." in job_id or len(job_id) > 80:
        return None
    listed = run_docker(
        ["ps", "--filter", "name=minos-", "--format", "{{.Names}}"],
        timeout=5,
        capture_stdout=True,
    )
    if listed is None or listed.returncode != 0:
        return None
    chunks: list[str] = []
    for name in (listed.stdout or "").splitlines():
        name = name.strip()
        if _NAME.fullmatch(name) is None:
            continue
        inspected = run_docker(
            ["inspect", "--format", "{{range .Mounts}}{{.Source}}\n{{end}}", name],
            timeout=5,
            capture_stdout=True,
        )
        if inspected is None or job_id not in (inspected.stdout or ""):
            continue
        logs = run_docker(["logs", "--tail", str(max(1, min(tail, 200))), name], timeout=5, capture_stdout=True)
        if logs is None:
            continue
        text = "\n".join(part for part in ((logs.stdout or "").strip(), (logs.stderr or "").strip()) if part)
        if text:
            chunks.append(text)
    if not chunks:
        return None
    return "\n".join(chunks)[-8000:]
