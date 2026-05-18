"""Discover the allowed-directory list for tool-using runners.

The advisor (``/get-advice``), the consultants engine (``/consultants``),
and the ``caliber-grounding-proxy`` all share the read-only tool layer
at :mod:`claude_hooks.caliber_proxy.tools`. Each tool path is checked
against a sandbox; until v1.8 that sandbox was the single ``cwd`` the
runner was invoked with, which broke any session that operates across
multiple working directories.

Claude Code already records the per-session allow-list in three places:

* ``~/.claude/settings.json`` — user-global, lives next to the global
  ``CLAUDE.md`` and ``settings.local.json``.
* ``<cwd>/.claude/settings.json`` — project-shared (sometimes checked
  into the repo).
* ``<cwd>/.claude/settings.local.json`` — project-local, gitignored.

Each file's ``permissions.additionalDirectories`` list (if present and
shaped like ``list[str]``) contributes one entry per element. Missing
files, malformed JSON, and wrong-shape values are all logged at DEBUG
and silently skipped — the discoverer never fails the runner.

The discoverer returns a stable, de-duplicated, realpath-canonical list
with the caller's primary ``cwd`` first, followed by settings entries
in file order, followed by ``--add-dir`` entries in order. Order is
stable so log lines stay readable across runs.

A single helper lives here so every caller (advisor, consultants,
caliber-grounding-proxy, plus future tool-using runners) reads the
same files in the same order — drift between callers would be a
quiet permissions bug.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("claude_hooks.allowed_roots")


# Path of the user-global Claude Code settings file. Pulled out as a
# module constant so tests can monkey-patch it (or pass
# ``settings_files=`` explicitly to skip the global file entirely).
USER_SETTINGS_PATH = "~/.claude/settings.json"

# Project-relative settings files. Read in the order they appear here;
# later files don't override earlier ones, they ADD to the allow-list.
PROJECT_SETTINGS_RELATIVE = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def _candidate_settings_files(cwd: str) -> list[str]:
    """Return the default three-file search path for a given cwd."""
    out = [os.path.expanduser(USER_SETTINGS_PATH)]
    for rel in PROJECT_SETTINGS_RELATIVE:
        out.append(os.path.join(cwd, rel))
    return out


def _read_additional_directories(path: str) -> list[str]:
    """Read ``permissions.additionalDirectories`` from one settings file.

    Returns ``[]`` for any failure: file missing, unreadable, JSON
    parse error, wrong-shape value. All failures log at DEBUG so the
    operator can see what's happening without the runner appearing
    to fail.
    """
    if not os.path.isfile(path):
        log.debug("settings file not present: %s", path)
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log.debug("settings file unreadable / malformed: %s: %s", path, e)
        return []
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return []
    raw = perms.get("additionalDirectories")
    if not isinstance(raw, list):
        if raw is not None:
            log.debug(
                "settings %s: additionalDirectories is %s, expected list",
                path, type(raw).__name__,
            )
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        else:
            log.debug(
                "settings %s: skipping non-string entry %r", path, item,
            )
    return out


def _canonical(path: str) -> Optional[str]:
    """Expand ``~``, realpath, and require it to be an existing dir.

    Returns ``None`` for entries that don't resolve to an actual
    directory — those are dropped from the allow-list with a DEBUG
    log so the operator can see them.
    """
    try:
        expanded = os.path.expanduser(path)
        real = os.path.realpath(expanded)
    except (OSError, ValueError) as e:
        log.debug("cannot canonicalise %r: %s", path, e)
        return None
    if not os.path.isdir(real):
        log.debug("allowed-root path is not a directory, skipping: %s", real)
        return None
    return real


def discover_allowed_roots(
    cwd: str,
    *,
    add_dirs: Sequence[str] = (),
    settings_files: Optional[Sequence[str]] = None,
) -> list[str]:
    """Return the de-duped, realpath-canonical allow-list.

    Args:
        cwd: the runner's primary working directory. Always lands first
            in the returned list.
        add_dirs: paths passed via ``--add-dir`` on the CLI (or the
            ``extra_roots`` field of an HTTP request). Appear after
            settings-file entries, in the order given.
        settings_files: override the three-file default search path
            (used by tests). When ``None``, reads
            ``~/.claude/settings.json``, ``<cwd>/.claude/settings.json``,
            and ``<cwd>/.claude/settings.local.json`` in that order.

    Returns:
        A list of absolute, realpath-canonical paths. Order:
        ``[cwd, *settings_entries, *add_dirs]``. Duplicates (by
        realpath) and missing directories are dropped. The primary
        ``cwd`` is always present even if it doesn't exist on disk
        — runners may pass a not-yet-created path on purpose.
    """
    files = (
        list(settings_files) if settings_files is not None
        else _candidate_settings_files(cwd)
    )

    # Collect ordered entries from settings + add_dirs.
    raw_entries: list[str] = []
    for f in files:
        raw_entries.extend(_read_additional_directories(f))
    raw_entries.extend(str(p) for p in add_dirs if p)

    # cwd is always first. Canonicalise it but don't require existence —
    # an advisor session may be started in a brand-new directory.
    cwd_real = os.path.realpath(os.path.expanduser(cwd))
    seen = {cwd_real}
    out = [cwd_real]
    for entry in raw_entries:
        real = _canonical(entry)
        if real is None or real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def render_for_log(
    roots: list[str], *,
    primary_label: str = "primary",
    display_roots: Optional[list[str]] = None,
) -> str:
    """Render the allow-list as a multi-line block for log output.

    Mirrors the shape used in the plan: ``primary:`` first, then
    ``extra:`` for the rest. Falls back to a single ``primary:`` line
    when there are no extras.

    2026-05-18: ``display_roots`` is an optional parallel list of the
    user-facing (pre-realpath) paths. When supplied, the log shows
    those instead of the realpath canonical forms — so a user who
    typed ``/shared/dev/laserRMT`` sees that in the log rather than
    ``/srv/dev-disk-by-label-opt/dev/laserRMT`` (the symlink-resolved
    form). When a display entry differs from the realpath, the
    realpath is shown in parentheses for forensic clarity. List must
    have the same length as ``roots``; mismatched lengths are
    ignored (defensive — fall back to roots as the display form).
    """
    if not roots:
        return f"{primary_label}: (none)"
    if display_roots is None or len(display_roots) != len(roots):
        display_roots = list(roots)

    def _format(display: str, real: str) -> str:
        return display if display == real else f"{display}  ({real})"

    lines = [f"{primary_label}: {_format(display_roots[0], roots[0])}"]
    if len(roots) > 1:
        lines.append("extra:")
        for d, r in zip(display_roots[1:], roots[1:]):
            lines.append(f"  {_format(d, r)}")
    return "\n".join(lines)


def discover_allowed_roots_with_display(
    cwd: str,
    *,
    add_dirs: Sequence[str] = (),
    settings_files: Optional[Sequence[str]] = None,
) -> tuple[list[str], list[str]]:
    """Same as :func:`discover_allowed_roots` but additionally returns
    the user-facing display form for each entry.

    Returns ``(real_paths, display_paths)`` parallel lists. The
    ``real_paths`` list is identical to what
    :func:`discover_allowed_roots` returns (used for de-dup +
    filesystem checks). The ``display_paths`` list holds the
    pre-realpath form (``~`` expanded, but symlinks NOT resolved)
    suitable for human-readable logging. They have the same
    length and the same order.

    Added 2026-05-18 for the consultants `allowed roots:` log so
    operators see ``/shared/dev/<x>`` (the path they typed in
    settings) rather than ``/srv/dev-disk-by-label-opt/dev/<x>``
    (the realpath after the ``/shared`` symlink resolves).
    """
    files = (
        list(settings_files) if settings_files is not None
        else _candidate_settings_files(cwd)
    )
    raw_entries: list[str] = []
    for f in files:
        raw_entries.extend(_read_additional_directories(f))
    raw_entries.extend(str(p) for p in add_dirs if p)

    cwd_display = os.path.expanduser(cwd)
    cwd_real = os.path.realpath(cwd_display)
    seen = {cwd_real}
    reals: list[str] = [cwd_real]
    displays: list[str] = [cwd_display]
    for entry in raw_entries:
        expanded = os.path.expanduser(entry)
        try:
            real = os.path.realpath(expanded)
        except (OSError, ValueError) as e:
            log.debug("cannot canonicalise %r: %s", entry, e)
            continue
        if not os.path.isdir(real):
            log.debug("allowed-root path is not a directory, skipping: %s", real)
            continue
        if real in seen:
            continue
        seen.add(real)
        reals.append(real)
        displays.append(expanded)
    return reals, displays
