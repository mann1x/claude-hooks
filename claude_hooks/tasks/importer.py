"""Import Claude Code's own task calls into task files.

Claude Code never kept backup_models' 849 tasks anywhere but the session
transcript: the list was not on disk, completed tasks are garbage-
collected, and the tools stopped being offered to newer models on
2026-08-27. This replays the ``TaskCreate`` / ``TaskUpdate`` calls in a
transcript (or an extract of one) into files:

- ids keep their number with the project prefix (``#856`` → ``bm-856``),
  so references in old notes still resolve;
- ``completed`` → done and ``deleted`` → cancelled, both straight to
  ``archive/``; ``in_progress`` → active;
- the final description is the Description, and every description it
  replaced is kept under ``## Earlier descriptions`` with its time —
  the session used descriptions as a running log (pivots, OOMs,
  scores), so the superseded text is history, not noise;
- status and title changes become Log lines at their original times;
  ``addBlockedBy`` / ``addBlocks`` become ``depends``.

A task id is matched to its create by the ``Task #N created`` tool
result, the only place the number appears. Re-running skips ids that
already have a file, so an interrupted import resumes.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from claude_hooks.tasks.model import (
    DESCRIPTION, Task, parse_stamp, stamp,
)

_CREATED = re.compile(r"Task #(\d+) created")
_STATUS = {"pending": "pending", "in_progress": "active",
           "completed": "done", "deleted": "cancelled"}
EARLIER = "Earlier descriptions"
_MARKERS = ("TaskCreate", "TaskUpdate", "Task #")


@dataclass
class Replayed:
    num: int
    created: str
    subject: str
    description: str
    status: str = "pending"
    updated: str = ""
    events: list = field(default_factory=list)       # (ts, text)
    earlier: list = field(default_factory=list)      # (ts, old description)
    blocked_by: list = field(default_factory=list)
    owner: str = ""


@dataclass
class ReplayResult:
    tasks: dict = field(default_factory=dict)        # num -> Replayed
    session: str = ""
    orphan_updates: int = 0
    unnumbered_creates: int = 0
    lines: int = 0


def _rows(path: Path) -> Iterator[dict]:
    """JSON rows that can hold a task call, without parsing the rest.

    A full transcript can be gigabytes; a substring test first keeps the
    import to the rows that matter.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not any(m in line for m in _MARKERS):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))


def replay(path: Path) -> ReplayResult:
    res = ReplayResult()
    pending: dict[str, tuple[str, dict]] = {}
    # addBlocks names the *other* task; applied once every task exists.
    reverse_blocks: list[tuple[int, int]] = []
    for row in _rows(Path(path)):
        res.lines += 1
        ts = row.get("timestamp") or ""
        if not res.session and row.get("sessionId"):
            res.session = str(row["sessionId"])
        for b in (row.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            kind, name = b.get("type"), b.get("name")
            if kind == "tool_use" and name == "TaskCreate":
                pending[b.get("id", "")] = (ts, b.get("input") or {})
            elif kind == "tool_result" and b.get("tool_use_id") in pending:
                m = _CREATED.search(_result_text(b))
                if not m:
                    continue
                cts, inp = pending.pop(b["tool_use_id"])
                if not inp.get("subject"):
                    continue
                n = int(m.group(1))
                res.tasks[n] = Replayed(
                    num=n, created=cts, updated=cts,
                    subject=str(inp.get("subject") or ""),
                    description=str(inp.get("description") or ""))
            elif kind == "tool_use" and name == "TaskUpdate":
                inp = b.get("input") or {}
                try:
                    n = int(str(inp.get("taskId") or "").lstrip("#"))
                except ValueError:
                    res.orphan_updates += 1
                    continue
                t = res.tasks.get(n)
                if t is None:
                    res.orphan_updates += 1
                    continue
                _apply(t, ts, inp, reverse_blocks)
    for src, dst in reverse_blocks:
        t = res.tasks.get(dst)
        if t is not None and src not in t.blocked_by:
            t.blocked_by.append(src)
    res.unnumbered_creates = len(pending)
    return res


def _apply(t: Replayed, ts: str, inp: dict, reverse_blocks: list) -> None:
    """One update → one log line, however many fields it changed."""
    t.updated = ts or t.updated
    parts: list[str] = []
    status = inp.get("status") or inp.get("state")
    if status:
        new = _STATUS.get(str(status), "pending")
        if new != t.status:
            parts.append(f"{t.status} → {new}")
            t.status = new
    subject = inp.get("subject")
    if subject and subject != t.subject:
        parts.append(f"retitled (was: {t.subject})")
        t.subject = str(subject)
    desc = inp.get("description")
    if desc is not None and desc != t.description:
        if t.description.strip():
            t.earlier.append((ts, t.description))
        parts.append("description rewritten")
        t.description = str(desc)
    owner = inp.get("owner")
    if owner and owner != t.owner:
        t.owner = str(owner)
        parts.append(f"owner {owner}")
    for dep in inp.get("addBlockedBy") or []:
        try:
            d = int(str(dep).lstrip("#"))
        except ValueError:
            continue
        if d not in t.blocked_by:
            t.blocked_by.append(d)
            parts.append(f"blocked by #{d}")
    for other in inp.get("addBlocks") or []:
        try:
            reverse_blocks.append((t.num, int(str(other).lstrip("#"))))
            parts.append(f"blocks #{other}")
        except ValueError:
            continue
    if inp.get("metadata"):
        parts.append("metadata " + json.dumps(inp["metadata"],
                                              ensure_ascii=False))
    if parts:
        t.events.append((ts, "; ".join(parts)))


def to_task(r: Replayed, prefix: str, session: str,
            known: set[int]) -> Task:
    created = stamp(parse_stamp(r.created)) if parse_stamp(r.created) else ""
    updated = stamp(parse_stamp(r.updated)) if parse_stamp(r.updated) else created
    deps = [f"{prefix}-{d}" for d in r.blocked_by if d in known]
    t = Task(id=f"{prefix}-{r.num}", title=" ".join(r.subject.split()),
             status=r.status, depends=deps, created=created,
             updated=updated, sessions=[session[:8]] if session else [],
             extra={"source": f"claude-code {session[:8]} #{r.num}"})
    t.set_section(DESCRIPTION, r.description.strip())
    if r.earlier:
        blocks = []
        for ts, text in reversed(r.earlier):
            when = parse_stamp(ts)
            label = f"{when:%Y-%m-%d %H:%M}Z" if when else ts
            blocks.append(f"### until {label}\n\n{text.strip()}")
        t.set_section(EARLIER, "\n\n".join(blocks))
    sid = session or ""
    created_at = parse_stamp(r.created)
    t.add_log("imported from Claude Code task #%d" % r.num, session=sid,
              when=created_at or None)
    for ts, text in r.events:
        t.add_log(text, session=sid, when=parse_stamp(ts) or created_at)
    t.updated = updated
    return t


def run_import(svc, transcript: str, *, dry_run: bool = False,
               session: str = "", out=None) -> int:
    out = out or sys.stdout
    path = Path(transcript)
    if not path.is_file():
        print(f"no such file: {path}", file=out)
        return 1
    res = replay(path)
    sid = session or res.session or path.stem.split("-task")[0]
    if not svc.dir.exists and not dry_run:
        svc.init()
    prefix = (svc.dir.prefix if svc.dir.exists
              else _would_be_prefix(svc))
    known = set(res.tasks)
    counts: dict[str, int] = {}
    for r in res.tasks.values():
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"{path.name}: {len(res.tasks)} tasks from session {sid[:8]} "
          f"→ {svc.dir.dir} (prefix {prefix})", file=out)
    print("  " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
          file=out)
    if res.orphan_updates or res.unnumbered_creates:
        print(f"  skipped: {res.orphan_updates} updates for unknown ids, "
              f"{res.unnumbered_creates} creates with no id", file=out)
    existing = {tf.id for tf in svc.dir.scan()} if svc.dir.exists else set()
    todo = [r for n, r in sorted(res.tasks.items())
            if f"{prefix}-{n}" not in existing]
    if len(todo) < len(res.tasks):
        print(f"  {len(res.tasks) - len(todo)} already imported; "
              f"{len(todo)} to go", file=out)
    if dry_run:
        open_ = [r for r in todo if r.status in ("pending", "active")]
        open_.sort(key=lambda r: r.updated, reverse=True)
        print(f"\n  open ({len(open_)}), most recently touched first:",
              file=out)
        for r in open_[:25]:
            print(f"    {prefix}-{r.num} [{r.status}] {r.updated[:10]} "
                  f"{r.subject[:90]}", file=out)
        if todo:
            sample = to_task(todo[-1], prefix, sid, known)
            print("\n  sample file:\n", file=out)
            print("    " + sample.to_markdown().replace("\n", "\n    "),
                  file=out)
        print("\n(dry run: nothing written)", file=out)
        return 0
    svc.board_on_write = False
    written = 0
    for r in todo:
        task = to_task(r, prefix, sid, known)
        path_, text = svc.dir.write(task)
        svc._index_one(task, path_, text)
        written += 1
        if written % 100 == 0:
            print(f"  {written}/{len(todo)}", file=out, flush=True)
    svc.board_on_write = True
    svc._refresh_board()
    print(f"imported {written} tasks into {svc.dir.dir}", file=out)
    return 0


def _would_be_prefix(svc) -> str:
    from claude_hooks.tasks.files import derive_prefix
    taken: set = set()
    if svc.index is not None:
        try:
            taken = svc.index.prefixes_taken(except_project=svc.project)
        except Exception:
            pass
    return derive_prefix(svc.dir.root.name, taken)

