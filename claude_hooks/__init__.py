"""claude-hooks: cross-platform Claude Code hooks for memory recall/store.

Pluggable provider backends: Qdrant, Memory KG, Postgres+pgvector, sqlite-vec.
Multiple backends can run simultaneously — the dispatcher fans out recall in
parallel and merges the result blocks into the prompt.
"""

from __future__ import annotations

from pathlib import Path as _Path
from typing import Optional


def _version_from_pyproject() -> Optional[str]:
    """Parse ``[project].version`` from a ``pyproject.toml`` that is an
    ancestor of this file and names this package. Returns ``None`` when
    no such pyproject is reachable (the normal site-packages case).
    """
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
    return None


def _resolve_version() -> str:
    """Resolve the package version at import time.

    Three sources, tried in order, so the constant can never drift
    from ``pyproject.toml`` again:

    1. Parse ``[project].version`` out of the ``pyproject.toml`` at the
       repo root, walking up from this file. For an editable / git-clone
       install (the model ``bin/claude-hook`` is built around) the
       source tree is the live, authoritative version — and it is an
       ancestor of this file. This MUST win over ``importlib.metadata``
       below, whose ``.dist-info`` is frozen at the moment
       ``pip install -e .`` last ran and goes stale after any
       code-only deploy.
    2. ``importlib.metadata.version("claude-hooks")`` — the canonical
       source for a non-editable ``pip install`` into site-packages,
       where there is no pyproject ancestor so source 1 yields nothing.
    3. Hard-coded fallback. Only reached on a broken / partial deploy
       (no pyproject reachable AND no metadata). Kept conservative so
       update-check reports "no update available" rather than
       hallucinating a newer build.

    Bug history: between v1.0.3 (when this constant landed) and
    v1.3.1, every release shipped with ``__version__ = "1.0.3"``
    because the cut procedure only updated ``pyproject.toml``. v1.3.2
    made the constant self-resolving. But v1.10.0 started running
    ``pip install -e .`` from the installer, which created a frozen
    ``.dist-info``; with ``importlib.metadata`` tried first, a v1.11.0
    code deploy that didn't re-run pip kept reporting the stale
    install-time 1.10.6. v1.11.x reorders the sources so the live
    source-tree pyproject wins for editable/clone installs.
    """
    from_pyproject = _version_from_pyproject()
    if from_pyproject:
        return from_pyproject

    try:
        from importlib.metadata import version as _md_version
        return _md_version("claude-hooks")
    except Exception:
        # importlib.metadata absent or package not installed via pip.
        pass

    # Final fallback. Bumped on each release as a defence-in-depth
    # value; the pyproject + metadata paths above are the canonical
    # sources.
    return "1.11.0"


__version__ = _resolve_version()
