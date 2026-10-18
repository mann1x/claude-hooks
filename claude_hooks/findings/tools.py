"""The ``finding-*`` MCP tools, served by both memory servers.

They work on the project's ledger files alone, so a host with no SQL
store has them too. ``finding-settle`` and ``finding-reopen`` take a
``by`` of ``user`` or ``assistant``: a council records candidates and
never settles (docs/PLAN-council-memory.md).
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from claude_hooks.findings.ledger import FindingNotFound, Ledger
from claude_hooks.findings.model import DECIDERS, SETTLED, STATUSES, Finding

_LIST = {"type": "array", "items": {"type": "string"}}


def tool_catalog() -> list[dict]:
    return [
        {"name": "finding-list",
         "description": (
             "List this project's council findings (the ledger under "
             ".claude-hooks/findings/): what councils claimed about the "
             "code and what was decided. Filter by status, or 'settled'."),
         "inputSchema": {"type": "object", "properties": {
             "status": {"type": "string",
                        "enum": [*STATUSES, "settled"]}}}},
        {"name": "finding-show",
         "description": "One finding in full: claim, reproduction, "
                        "evidence, decision and log.",
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "string"}}, "required": ["id"]}},
        {"name": "finding-record",
         "description": (
             "Record a claim about the code as a candidate finding, or add "
             "evidence to the finding it restates (matched by place and "
             "words). Never changes a status."),
         "inputSchema": {"type": "object", "properties": {
             "title": {"type": "string", "description": "one line"},
             "claim": {"type": "string",
                       "description": "what is wrong, and when"},
             "paths": {**_LIST, "description": "path:line places"},
             "symbols": _LIST,
             "severity": {"type": "string", "enum": ["H", "M", "L"]},
             "evidence": {"type": "string",
                          "description": "the run or read that shows it"},
             "reproduce": {"type": "string",
                           "description": "the command that shows it"},
             "council": {"type": "string",
                         "description": "council id, when one found it"}},
             "required": ["title", "claim"]}},
        {"name": "finding-settle",
         "description": (
             "Decide a finding: confirmed, refuted, by_design or fixed, "
             "with the reason (the run, the commit, where the design says "
             "so). Only the user or the assistant decides; fixed needs the "
             "commit."),
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "string"},
             "status": {"type": "string", "enum": list(SETTLED)},
             "reason": {"type": "string"},
             "by": {"type": "string", "enum": list(DECIDERS)},
             "commit": {"type": "string"},
             "task": {"type": "string",
                      "description": "task id tracking the fix"}},
             "required": ["id", "status", "reason", "by"]}},
        {"name": "finding-reopen",
         "description": "Make a settled finding a candidate again, with "
                        "the new evidence that puts the decision in doubt.",
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "string"}, "reason": {"type": "string"},
             "by": {"type": "string", "enum": list(DECIDERS)}},
             "required": ["id", "reason", "by"]}},
        {"name": "finding-note",
         "description": "Append a line to a finding's log.",
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "string"}, "text": {"type": "string"}},
             "required": ["id", "text"]}},
    ]


TOOL_NAMES = tuple(t["name"] for t in tool_catalog())


def row(f: Finding) -> str:
    where = f" · {f.paths[0]}" if f.paths else ""
    seen = f" · {len(f.councils)} council(s)" if f.councils else ""
    return f"{f.id} [{f.status} {f.severity}] {f.title}{where}{seen}"


def show(f: Finding, ledger: Optional[Ledger] = None) -> str:
    head = row(f)
    if ledger is not None:
        try:
            head += f"\nfile: {ledger.path_for(f.id).relative_to(ledger.root)}"
        except ValueError:
            pass
    if f.settled:
        head += f"\nsettled by {f.settled_by} at {f.settled_at}"
    if f.commit:
        head += f"\ncommit: {f.commit}"
    if f.task:
        head += f"\ntask: {f.task}"
    return head + "\n\n" + f.to_markdown().split("\n---\n", 1)[-1].strip()


class FindingTools:
    def __init__(self, ledger_factory: Callable[[], Ledger]):
        self._factory = ledger_factory
        self._ledger: Optional[Ledger] = None

    @property
    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = self._factory()
        return self._ledger

    def call(self, name: str, args: dict) -> str:
        try:
            return self._call(name, args or {})
        except FindingNotFound as e:
            return f"{e}. finding-list shows what exists."
        except ValueError as e:
            return f"Not done: {e}"

    def _call(self, name: str, a: dict) -> str:
        lg = self.ledger
        if name == "finding-list":
            fs = lg.list(a.get("status") or None)
            if not fs:
                return "No findings" + (f" with status {a['status']}"
                                        if a.get("status") else "") + "."
            return "\n".join(row(f) for f in fs)
        if name == "finding-show":
            return show(lg.read(str(a["id"])), lg)
        if name == "finding-record":
            f, created = lg.record(
                a.get("title", ""), a.get("claim", ""),
                paths=a.get("paths") or (), symbols=a.get("symbols") or (),
                severity=a.get("severity") or "M",
                evidence=a.get("evidence", ""), reproduce=a.get("reproduce", ""),
                council=a.get("council", ""))
            return (("recorded " if created else "matched; evidence added to ")
                    + row(f))
        if name == "finding-settle":
            f = lg.settle(str(a["id"]), a.get("status", ""), a.get("reason", ""),
                          by=a.get("by", ""), commit=a.get("commit", ""),
                          task=a.get("task", ""))
            return f"settled {row(f)}"
        if name == "finding-reopen":
            f = lg.reopen(str(a["id"]), a.get("reason", ""), by=a.get("by", ""))
            return f"reopened {row(f)}"
        if name == "finding-note":
            f = lg.note(str(a["id"]), a.get("text", ""))
            return f"noted on {f.id}"
        return f"unknown tool {name}"


def tools_for_cwd(cwd: Optional[str] = None) -> FindingTools:
    """Bound to the session's project (``CLAUDE_PROJECT_DIR``)."""
    return FindingTools(lambda: Ledger.for_cwd(
        cwd or os.environ.get("CLAUDE_PROJECT_DIR")))


__all__ = ["tool_catalog", "TOOL_NAMES", "FindingTools", "tools_for_cwd", "row", "show"]
