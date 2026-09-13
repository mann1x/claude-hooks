#!/usr/bin/env python3
"""Reconcile ``cclsp.json`` with the language servers actually installed.

``install.py`` writes a starter ``cclsp.json`` from whatever is present
at setup time and never revisits it. Servers installed afterwards are
therefore never wired, and nothing says so: an unmapped extension means
no server claims the file, so the engine returns an empty diagnostic
list — which is exactly what a clean file returns.

That is not hypothetical. On 2026-09-13 pandorum had nine servers
installed and four of them unmapped — typescript-language-server,
bash-language-server, lua-language-server and zls — because they were
installed after the config was generated. TypeScript had been silently
dead ever since.

This script is the reconciliation step:

* every server in :data:`claude_hooks.lang_servers.SPECS` that resolves
  on ``PATH`` gets an entry,
* existing entries keep their command (a hand-tuned one is not
  overwritten) and gain only the extensions they are missing,
* a server that is *not* installed is never added, and an entry for a
  server that has since been uninstalled is reported but left alone —
  removing it would break a host that shares the file over a checkout.

Usage::

    scripts/sync_cclsp.py              # report, change nothing
    scripts/sync_cclsp.py --write      # apply
    scripts/sync_cclsp.py --write --project /path/to/repo

Exit code is 1 in report mode when something is missing, so it can gate
a check; 0 after a successful ``--write``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from claude_hooks.lang_servers import SPECS  # noqa: E402
from claude_hooks.lsp_engine.lsp import language_id_for  # noqa: E402


def _load(path: Path) -> dict:
    if not path.is_file():
        return {"servers": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")
    data.setdefault("servers", [])
    return data


def reconcile(cfg: dict) -> tuple[dict, list[str]]:
    """Return ``(new_cfg, notes)``. Pure — does no I/O."""
    by_bin = {
        s["command"][0]: s
        for s in cfg["servers"]
        if s.get("command")
    }
    notes: list[str] = []

    for spec in SPECS:
        resolved = shutil.which(spec.bin)
        entry = by_bin.get(spec.bin)
        if resolved is None:
            if entry is not None:
                notes.append(
                    f"  note   {spec.bin}: mapped but not installed — left "
                    f"alone (another host may share this file)")
            continue

        # An extension the engine would announce as "plaintext" is worse
        # than an unmapped one: the server accepts the document and
        # silently declines to analyse it.
        wanted = [e for e in spec.extensions
                  if language_id_for(f"x.{e}") != "plaintext"]
        skipped = sorted(set(spec.extensions) - set(wanted))
        if skipped:
            notes.append(
                f"  SKIP   {spec.bin}: {','.join(skipped)} have no languageId "
                f"in lsp.py — add them there first")

        if entry is None:
            cfg["servers"].append({
                "extensions": wanted,
                "command": list(spec.cclsp_command),
            })
            notes.append(f"  ADD    {spec.bin} -> {','.join(wanted)}")
            continue

        have = set(entry.get("extensions") or [])
        missing = [e for e in wanted if e not in have]
        if missing:
            entry["extensions"] = sorted(have | set(missing))
            notes.append(f"  EXTEND {spec.bin} += {','.join(missing)}")

    return cfg, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default=".",
                    help="project root holding cclsp.json (default: cwd)")
    ap.add_argument("--write", action="store_true",
                    help="apply the changes (default: report only)")
    a = ap.parse_args()

    path = Path(a.project).resolve() / "cclsp.json"
    cfg = _load(path)
    before = json.dumps(cfg, sort_keys=True)
    cfg, notes = reconcile(cfg)
    changed = json.dumps(cfg, sort_keys=True) != before

    print(f"cclsp.json — {path}")
    if not notes:
        print("  in sync with the installed servers")
        return 0
    for n in notes:
        print(n)

    if not changed:
        return 0
    if not a.write:
        print("\n  report only — re-run with --write to apply")
        return 1

    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"\n  written. Restart the engine to pick it up:\n"
          f"    python -m claude_hooks.lsp_engine restart --project {path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
