# -*- mode: python ; coding: utf-8 -*-
"""Frozen Ubuntu client. The staged tree contains this spec under deployment/ubuntu."""

from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]
tuner = root / "minos-lan-tuner"

a = Analysis(
    [str(tuner / "apps" / "worker" / "windows_client.py")],
    pathex=[str(tuner), str(root)],
    binaries=[],
    datas=[(str(root / "templates" / "gatk.py"), "templates")],
    hiddenimports=[
        "apps.worker.frozen_commit",
        "apps.worker.runner",
        "apps.worker.doctor",
        "apps.worker.cache",
        "apps.worker.settings",
        "shared.schemas",
        "shared.atomic",
        "shared.hashing",
        "shared.docker_paths",
        "integrations.minos.engine",
        "integrations.minos.pin",
        "integrations.minos.scoring",
        "templates",
        "templates.gatk",
        "templates.tool_params",
        "templates._common",
        "utils",
        "utils.scoring",
        "utils.scoring_version",
        "yaml",
        "httpx",
        "httpcore",
        "certifi",
        "pydantic",
        "pydantic_core",
        "tkinter",
        "tkinter.ttk",
        "tkinter.font",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["optuna", "fastapi", "uvicorn", "sqlalchemy", "alembic"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MinosClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
