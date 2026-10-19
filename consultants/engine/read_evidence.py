"""Evidence by ref: a judge reads the bytes a lane read (plan G4).

On the typed protocol a researcher's claim names the ``path`` and
``line`` it rests on. The critic and the synthesizer get one more tool,
:data:`EVIDENCE_TOOL`, that answers **only from the run's read cache**
(``consultants/server/tool_cache.py``): the lines some lane's
``read_file`` returned in this run, exactly as that lane saw them. It
never reads the disk. A range no lane read is said to be unread, which
is itself the finding (a claim citing lines nobody read).

Offered only with ``[protocol] typed`` on and only to a role that has
tools; without either the role's tools are unchanged (M12 parity).
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

EVIDENCE_TOOL = "evidence"
#: Lines one call returns.
MAX_LINES = 200

_NUMBERED = re.compile(r"^\s*(\d+): ?(.*)$")


def spec() -> dict:
    return {"type": "function", "function": {
        "name": EVIDENCE_TOOL,
        "description": (
            "Show lines of a file exactly as a researcher lane read them in "
            "this run (from the run's read cache; the disk is not read). Use "
            "it to check a report's claim against the bytes it rests on. "
            "Lines no lane read are listed as unread."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}},
            "required": ["path", "start_line"]}}}


def _norm(path: str, cwd: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(cwd or "", path)
    return os.path.normpath(p)


def lines_read(cache, path: str, cwd: str) -> dict[int, str]:
    """Line number -> text, from every shared ``read_file`` result for
    ``path`` in ``cache``. Lane-private reads (a lane whose view diverged)
    are left out: they describe that lane's view, not the tree."""
    want = _norm(path, cwd)
    out: dict[int, str] = {}
    for key, text in cache.snapshot():
        scope, name, args, kcwd = key[0], key[1], key[2], key[3]
        if scope is not None or name != "read_file":
            continue
        try:
            p = (json.loads(args) or {}).get("path")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, str) or _norm(p, kcwd) != want:
            continue
        for row in str(text).splitlines()[1:]:
            m = _NUMBERED.match(row)
            if m:
                out.setdefault(int(m.group(1)), m.group(2))
    return out


def answer(cache, raw_args: str, cwd: str) -> str:
    try:
        a = json.loads(raw_args or "{}") or {}
        path = str(a["path"])
        s = int(a["start_line"])
        e = int(a.get("end_line") or s)
    except (KeyError, TypeError, ValueError, AttributeError):
        return "error: `path` and an integer `start_line` are required"
    if s < 1 or e < s:
        return "error: start_line must be ≥ 1 and end_line ≥ start_line"
    e = min(e, s + MAX_LINES - 1)
    got = lines_read(cache, path, cwd)
    if not got:
        return (f"error: no lane read {path} in this run; a claim citing it "
                "rests on nothing the council saw")
    rows, unread = [], []
    for n in range(s, e + 1):
        if n in got:
            rows.append(f"{n:>6}: {got[n]}")
        else:
            unread.append(n)
    head = f"{path}:{s}-{e} as read by the lanes"
    if unread:
        head += f"  [unread by any lane: {_ranges(unread)}]"
    return head + ("\n" + "\n".join(rows) if rows else "")


def _ranges(ns: list[int]) -> str:
    out, start, prev = [], ns[0], ns[0]
    for n in ns[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(out)


def find_cache(executor) -> Optional[object]:
    """The run's read cache behind ``executor`` (walks the wrappers)."""
    seen = 0
    while executor is not None and seen < 8:
        if hasattr(executor, "snapshot") and hasattr(executor, "clear_lane"):
            return executor
        executor = (getattr(executor, "inner", None)
                    or getattr(executor, "_executor", None)
                    or getattr(executor, "__wrapped__", None))
        seen += 1
    return None


def with_evidence(state: Optional[dict], tool_specs: Optional[list],
                  tool_executor: Optional[Callable]
                  ) -> tuple[Optional[list], Optional[Callable]]:
    """``tool_specs`` / ``tool_executor`` with :data:`EVIDENCE_TOOL` when
    the typed protocol is on, the role has tools and the run has a read
    cache; unchanged otherwise."""
    if not ((state or {}).get("runtime_control") or {}).get("typed_protocol") \
            or not tool_specs or tool_executor is None:
        return tool_specs, tool_executor
    cache = find_cache(tool_executor)
    if cache is None:
        return tool_specs, tool_executor
    specs = list(tool_specs) + [spec()]

    def execute(name: str, raw_args: str, cwd: str, **kw) -> str:
        if name != EVIDENCE_TOOL:
            return tool_executor(name, raw_args, cwd, **kw)
        return answer(cache, raw_args, cwd)

    return specs, execute


__all__ = ["EVIDENCE_TOOL", "MAX_LINES", "spec", "lines_read", "answer",
           "find_cache", "with_evidence"]
