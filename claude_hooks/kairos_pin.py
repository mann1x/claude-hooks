"""
Pin GrowthBook feature flags in ``~/.claude.json`` against silent rollback.

Some Claude Code features ship behind GrowthBook flags. The CLI fetches
the flag set from GrowthBook Cloud at startup and writes it back into
``~/.claude.json`` under ``cachedGrowthBookFeatures``. When a flag we
care about is shipped as ``false`` for an account, every fresh session
overwrites a manual flip on disk.

The most important one is ``tengu_kairos_cron_durable``: when ``false``,
``CronCreate(durable=true)`` is silently downgraded to session-only --
the safety-net cron dies the moment the Claude Code session ends, even
when the caller explicitly asked for a persisted job. Users running paid
remote pods / GPUs can lose money to this when their watchdog cron dies
overnight.

This module provides an idempotent re-pin operation. It is **opt-in**
and intended to be invoked by:

- a systemd path-watcher on ``~/.claude.json`` (preferred -- re-pins
  within ms of any CC write), installed by ``install.py``
- a one-shot CLI: ``python -m claude_hooks.kairos_pin``

The module never raises on bad input -- corrupt JSON, missing file, or
missing ``cachedGrowthBookFeatures`` are all silent no-ops, so a
mis-fired path trigger never breaks anything.

This is a workaround. When Anthropic rolls a flag out properly we can
remove the corresponding pin (or leave it as a harmless idempotent
no-op).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

log = logging.getLogger("claude_hooks.kairos_pin")

DEFAULT_CONFIG_PATH = Path.home() / ".claude.json"

# Flags we want pinned to True regardless of what GrowthBook shipped.
# Keep this list small and focused on safety-impacting features.
DEFAULT_PINS: dict[str, bool] = {
    "tengu_kairos_cron_durable": True,
}


def pin_flags(
    *,
    pins: dict[str, bool] | None = None,
    path: Path = DEFAULT_CONFIG_PATH,
) -> Iterable[tuple[str, bool, bool]]:
    """Idempotently pin GrowthBook flags in ``~/.claude.json``.

    Yields ``(flag_name, old_value, new_value)`` for each flag that was
    actually changed. Returns silently when the file is missing,
    unreadable, or contains no ``cachedGrowthBookFeatures`` key -- those
    are not error conditions, just "nothing to do here."

    Atomicity: the rewrite goes through ``<path>.kairos-pin.tmp`` then
    ``os.replace`` so a crash mid-write never leaves a half-truncated
    config behind.
    """
    pins = pins if pins is not None else DEFAULT_PINS
    if not pins:
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("kairos_pin: cannot read %s: %s", path, exc)
        return

    gb = data.get("cachedGrowthBookFeatures")
    if not isinstance(gb, dict):
        return

    changes: list[tuple[str, bool, bool]] = []
    for flag, want in pins.items():
        current = gb.get(flag)
        if current != want:
            changes.append(
                (flag, bool(current) if current is not None else False, want)
            )
            gb[flag] = want

    if not changes:
        return

    try:
        tmp = path.with_suffix(path.suffix + ".kairos-pin.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("kairos_pin: failed to write %s: %s", path, exc)
        return

    for flag, old, new in changes:
        log.info("kairos_pin: flipped %s: %s -> %s", flag, old, new)
        yield (flag, old, new)


def is_pin_needed(path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Return True when at least one default-pinned flag is currently
    not at its desired value (i.e. installing the pin would actually do
    something useful on this host).

    Used by ``install.py`` to decide whether to propose the systemd
    path-watcher. Silent on read errors -- treats them as "no pin
    needed" so we don't push installation on hosts where the flag file
    isn't readable.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return False
    gb = data.get("cachedGrowthBookFeatures")
    if not isinstance(gb, dict):
        return False
    for flag, want in DEFAULT_PINS.items():
        if gb.get(flag) != want:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. ``python -m claude_hooks.kairos_pin``.

    Arguments:
        --path PATH   override the config file (default ``~/.claude.json``)
        --check       exit 0 if no pin is needed, 1 if at least one flag
                      would be flipped. Does not modify anything.
        --quiet       suppress per-flip stdout output

    Default behavior (no flags) writes any needed flips and reports them
    on stdout, one per line.
    """
    args = argv if argv is not None else sys.argv[1:]
    path = DEFAULT_CONFIG_PATH
    check_only = False
    quiet = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--path":
            i += 1
            path = Path(args[i])
        elif a == "--check":
            check_only = True
        elif a == "--quiet":
            quiet = True
        elif a in ("-h", "--help"):
            print(main.__doc__)
            return 0
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 2
        i += 1

    if check_only:
        return 1 if is_pin_needed(path) else 0

    flipped = list(pin_flags(path=path))
    if not quiet:
        for flag, old, new in flipped:
            print(f"kairos_pin: {flag}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
