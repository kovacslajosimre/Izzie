"""Repo-gyökér és env var alapú útvonal-feloldás.

Az IZZIE_PERSONA és IZZIE_DB_PATH ugyanazt a mintát követi (lásd CLAUDE.md):
env var, vagy ha nincs megadva, egy repo-gyökérhez képest relatív alapérték.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_path(env_var: str, default_relative: str) -> Path:
    """Env var vagy alapértelmezett relatív útvonal feloldása a repo gyökeréhez képest."""
    value = os.getenv(env_var)
    path = Path(value) if value else Path(default_relative)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path
