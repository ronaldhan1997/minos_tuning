"""Locate this tuning project and the Minos checkout it calls.

The tuner lives in ``minos_subnet/tuning/minos_tuning``. Ancestors are searched
until the directory that contains ``start-miner.sh`` and ``templates/``.
``MINOS_REPO_ROOT`` overrides that when the checkout is somewhere else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent


def minos_root() -> Path:
    """Directory that contains ``start-miner.sh`` and ``templates/``."""
    override = os.environ.get("MINOS_REPO_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        root = None
        for candidate in (project_root(), *project_root().parents):
            if (candidate / "start-miner.sh").is_file() and (candidate / "templates").is_dir():
                root = candidate
                break
        if root is None:
            root = project_root().parent
    if not (root / "start-miner.sh").is_file() or not (root / "templates").is_dir():
        raise RuntimeError(
            f"Minos checkout not found at {root}. "
            "Set MINOS_REPO_ROOT to the directory that contains start-miner.sh."
        )
    return root


def ensure_minos_importable() -> Path:
    """Put the Minos checkout on ``sys.path`` so ``templates`` and ``utils`` import."""
    root = minos_root()
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root
