"""Where the hackathon dataset lives.

The dataset is not part of this repository. It is located, in order, from:

1. the ``AFC_INSTITUTIONS_DIR`` environment variable: the folder that holds
   ``bank/``, ``emoney/``, ``investment/`` and ``payments_fx/``;
2. ``<repo>/institutions`` (if someone places or links the data there);
3. ``<repo>/../hackathon/institutions`` (the extracted hackathon zip next to
   this repository).

Every public loader also accepts an explicit path, so this lookup is only a
convenience for the CLI and the tests. No absolute path is built in.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "AFC_INSTITUTIONS_DIR"
REPO_ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = (REPO_ROOT / "institutions", REPO_ROOT.parent / "hackathon" / "institutions")


def institutions_dir() -> Path | None:
    """The dataset's ``institutions`` folder, or ``None`` if it cannot be found."""
    env = os.environ.get(ENV_VAR)
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    for p in _CANDIDATES:
        if p.is_dir():
            return p.resolve()
    return None


def resolve_institution(path_or_name: str | Path) -> Path:
    """Accept an institution folder path, or a bare name such as ``"bank"``."""
    p = Path(path_or_name).expanduser()
    if p.is_dir():
        return p.resolve()
    base = institutions_dir()
    if base is not None and (base / str(path_or_name)).is_dir():
        return (base / str(path_or_name)).resolve()
    raise FileNotFoundError(
        f"institution folder {str(path_or_name)!r} not found; pass a full path or set {ENV_VAR}")
