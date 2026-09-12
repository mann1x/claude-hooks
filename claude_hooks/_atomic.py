"""Atomic file replacement that survives concurrent writers.

The obvious spelling of an atomic write::

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)

is atomic only for a *single* writer. The temp path is derived from the
destination, so every process racing to update the same file picks the
identical name. They interleave like this::

    A: write  <path>.tmp
    B: write  <path>.tmp      # same file, clobbers A's bytes
    A: replace <path>.tmp -> <path>
    B: replace <path>.tmp -> <path>   # FileNotFoundError: A consumed it

The loser raises ``[Errno 2] ... .json.tmp -> ... .json`` and, because
these are best-effort caches, the error is swallowed and its update is
silently dropped. On solidpc this fired ~28 times in two days across
``decay`` and ``hyde_cache`` -- so decay history was being lost and the
HyDE cache kept re-paying for cloud calls it had already made.

Giving each writer a unique temp file in the destination's directory
removes the shared name. Last writer wins, which is the correct
semantics for a cache: every writer holds a complete document, so no
update can be torn, only superseded.

The temp file must live in the destination's directory -- ``os.replace``
is only atomic within a filesystem, and a system temp dir is routinely a
different mount (tmpfs, here).
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

__all__ = ["write_text_atomic"]

# Windows only. POSIX ``rename(2)`` is atomic against concurrent
# renames; Windows ``MoveFileEx`` is not, and fails ERROR_ACCESS_DENIED
# (PermissionError, errno 13) when another writer holds the destination
# open for even an instant — which is exactly what a storm of
# last-writer-wins updates looks like. The condition is transient: the
# other replace completes, its handle closes, and the next attempt
# succeeds. Antivirus and search indexers open files behind our back and
# produce the same error, so this is not purely a self-contention guard.
#
# ~0.6 s of total patience across 12 tries, which is far longer than any
# observed collision and still short enough that a genuinely locked file
# fails inside a hook's timeout budget rather than hanging it.
_REPLACE_ATTEMPTS = 12
_REPLACE_BACKOFF_S = 0.01
_REPLACE_BACKOFF_MAX_S = 0.1


def _replace_with_retry(tmp: Path, dest: Path) -> None:
    """``os.replace`` with a bounded retry on Windows sharing errors."""
    delay = _REPLACE_BACKOFF_S
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            # On POSIX this is a real permission problem and retrying
            # only delays the report; there is no sharing-violation
            # equivalent to wait out.
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_BACKOFF_MAX_S)


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text``, safely against concurrent writers.

    Creates the parent directory if needed. Raises ``OSError`` on
    failure like the plain write it replaces, so existing callers keep
    their own logging; the unique temp file is never left behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        _replace_with_retry(tmp, path)
    except BaseException:
        # A unique name means a failed attempt leaves unique litter, so
        # unlike the shared-name version this cleanup actually matters.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
