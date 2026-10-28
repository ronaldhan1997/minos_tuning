"""Serve the compiled dashboard. API routes stay on the same origin."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

DIST = Path(__file__).resolve().parents[2] / "apps" / "dashboard" / "dist"

_FALLBACK = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Minos tuner</title></head>
<body style="font-family:sans-serif;margin:2rem;max-width:40rem">
<h1>Dashboard bundle is not built</h1>
<p>The coordinator API is running. Build the browser app on the Ubuntu host, then restart:</p>
<pre>cd tuning/minos_tuning/minos-lan-tuner/apps/dashboard
npm install
npm run build</pre>
<p>A no-JavaScript status page remains at <a href="/legacy">/legacy</a>.</p>
</body></html>
"""


def install_ui(app: FastAPI) -> None:
    assets = DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    def dashboard_index() -> HTMLResponse:
        return _index()

    @app.get("/login", include_in_schema=False)
    @app.get("/overview", include_in_schema=False)
    @app.get("/experiments", include_in_schema=False)
    @app.get("/experiments/{experiment_id}", include_in_schema=False)
    @app.get("/workers", include_in_schema=False)
    @app.get("/jobs", include_in_schema=False)
    @app.get("/jobs/{job_id}", include_in_schema=False)
    @app.get("/datasets", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    @app.get("/activity", include_in_schema=False)
    def dashboard_route() -> HTMLResponse:
        return _index()


def _index() -> HTMLResponse:
    index = DIST / "index.html"
    if index.is_file():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse(_FALLBACK)
