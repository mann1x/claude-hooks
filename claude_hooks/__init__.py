"""claude-hooks: cross-platform Claude Code hooks for memory recall/store.

Pluggable provider backends: Qdrant, Memory KG, Postgres+pgvector, sqlite-vec.
Multiple backends can run simultaneously — the dispatcher fans out recall in
parallel and merges the result blocks into the prompt.
"""

from __future__ import annotations

from pathlib import Path as _Path


def _resolve_version() -> str:
    """Resolve the package version at import time.

    Three sources, tried in order, so the constant can never drift
    from ``pyproject.toml`` again:

    1. ``importlib.metadata.version("claude-hooks")`` — populated when
       the package was installed via ``pip install`` (editable or not).
       This is the canonical source on pip-managed installs.
    2. Parse ``[project].version`` out of ``pyproject.toml`` at the
       repo root, walking up from this file. This covers the common
       case where users run the hooks straight from a ``git clone``
       without ever invoking pip — that's the install model the
       ``bin/claude-hook`` shim is designed for.
    3. Hard-coded fallback. Only reached on a broken / partial deploy
       (no metadata AND no pyproject reachable). Kept conservative so
       update-check reports "no update available" rather than
       hallucinating a newer build.

    Bug history: between v1.0.3 (when this constant landed) and
    v1.3.1, every release shipped with ``__version__ = "1.0.3"``
    because the cut procedure only updated ``pyproject.toml``. The
    update-check banner consequently misreported the running version
    for months. v1.3.2 makes the constant self-resolving so the
    drift can't recur.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version as _md_version
        return _md_version("claude-hooks")
    except Exception:
        # importlib.metadata absent (pre-3.8 won't happen — we require
        # 3.9+) or package not installed via pip. Fall through.
        pass

    # Source-tree fallback: walk up from this file looking for a
    # ``pyproject.toml`` that names this package.
    here = _Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        # Tiny hand-rolled parser — avoid importing ``tomllib`` (3.11+)
        # or ``tomli`` so this stays stdlib-only on 3.9/3.10. We only
        # need the ``version = "X.Y.Z"`` line from ``[project]``.
        in_project = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                in_project = line == "[project]"
                continue
            if not in_project:
                continue
            if line.startswith("version"):
                # ``version = "X.Y.Z"`` → extract the quoted value.
                _, _, rhs = line.partition("=")
                rhs = rhs.strip().strip('"').strip("'")
                if rhs:
                    return rhs
        # Found a pyproject but no [project].version — keep looking up.

    # Final fallback. Bumped on each release as a defence-in-depth
    # value; the metadata + pyproject paths above are the canonical
    # sources.
    return "0.0.0+unknown"


__version__ = _resolve_version()
