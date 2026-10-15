"""The researcher reports as the critic and synthesizer read them (plan D8;
Cerebriline ``batch-report.ts:49``, ``agent-reports.ts:18-23``).

Reports that fit :data:`LANE_REPORT_BUDGET_CHARS` together are handed over
exactly as before — the prompt is byte-identical, so the parity cohorts
hold. Past it, compaction used to fold the reports by position, so the
lanes at the end were the ones a reader lost, without knowing they existed.
Instead:

1. an index of every report first — number, ok / failed, its first line,
   its size — so no lane goes unseen;
2. whole reports, in order, while they fit (a later, shorter one still goes
   in when an earlier one does not);
3. the rest named, with ``read_lane_report`` to page one in when the role
   has tools.

A report is never cut: a half report reads as a whole one.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

LANE_REPORT_BUDGET_CHARS = 28_000
INDEX_LINE_CHARS = 120
PAGER_TOOL = "read_lane_report"

#: The prefix every researcher tombstone opens with (``council.py``).
_FAILED_PREFIXES = ("(researcher lane failed", "(researcher lane produced no")


def _first_line(report: str) -> str:
    for line in (report or "").splitlines():
        flat = " ".join(line.strip().lstrip("#*->").split())
        if flat:
            if len(flat) > INDEX_LINE_CHARS:
                flat = flat[:INDEX_LINE_CHARS - 1] + "…"
            return flat
    return "(empty)"


def _status(report: str) -> str:
    return ("failed" if (report or "").lstrip().startswith(_FAILED_PREFIXES)
            else "ok")


def fits(reports: list[str], budget: int = LANE_REPORT_BUDGET_CHARS) -> bool:
    return sum(len((r or "").strip()) for r in reports) <= budget


def omitted(reports: list[str], budget: int = LANE_REPORT_BUDGET_CHARS
            ) -> list[int]:
    """1-based numbers of the reports :func:`render` leaves out."""
    return [] if fits(reports, budget) else _layout(reports, budget)[1]


def _index(reports: list[str]) -> str:
    lines = [f"RESEARCH INDEX ({len(reports)} reports — every one is listed "
             "here; whole reports follow while they fit):"]
    for i, r in enumerate(reports, start=1):
        lines.append(f"- report {i} [{_status(r)}, {len(r.strip()):,} chars]: "
                     f"{_first_line(r)}")
    return "\n".join(lines)


def _layout(reports: list[str], budget: int) -> tuple[list[int], list[int]]:
    left = budget - len(_index(reports))
    shown, hidden = [], []
    for i, r in enumerate(reports, start=1):
        size = len(r.strip()) + 40  # its header line
        if size <= left:
            shown.append(i)
            left -= size
        else:
            hidden.append(i)
    return shown, hidden


def render(reports: list[str], *, pager: bool,
           budget: int = LANE_REPORT_BUDGET_CHARS) -> Optional[list[str]]:
    """The prompt parts for ``reports``, or ``None`` when they fit and the
    caller's own (unchanged) rendering applies."""
    if fits(reports, budget):
        return None
    shown, hidden = _layout(reports, budget)
    parts = ["\n" + _index(reports)]
    for i in shown:
        parts.append(f"\nRESEARCHER REPORT {i} of {len(reports)}:\n"
                     f"{reports[i - 1].strip()}")
    if hidden:
        named = ", ".join(str(i) for i in hidden)
        how = (f"Call `{PAGER_TOOL}` with its number to read one in full "
               "before you rely on it." if pager else
               "Their index lines above are all you have of them: say so "
               "where a conclusion depends on one.")
        parts.append(f"\nNOT SHOWN for space: report{'s' if len(hidden) > 1 else ''} "
                     f"{named}. {how}")
    return parts


def pager_spec(count: int) -> dict:
    return {"type": "function", "function": {
        "name": PAGER_TOOL,
        "description": ("Read one researcher report in full, by its number "
                        f"in the research index (1 to {count})."),
        "parameters": {"type": "object", "properties": {
            "report": {"type": "integer",
                       "description": "The report's number in the index."}},
            "required": ["report"]}}}


def with_pager(reports: list[str], tool_specs: Optional[list],
               tool_executor: Optional[Callable[[str, str, str], str]]
               ) -> tuple[Optional[list], Optional[Callable]]:
    """``tool_specs`` / ``tool_executor`` with :data:`PAGER_TOOL` added
    when the role has tools and some report is left out; unchanged
    otherwise."""
    if not tool_specs or tool_executor is None or not omitted(reports):
        return tool_specs, tool_executor
    specs = list(tool_specs) + [pager_spec(len(reports))]

    def execute(name: str, raw_args: str, cwd: str) -> str:
        if name != PAGER_TOOL:
            return tool_executor(name, raw_args, cwd)
        try:
            n = int((json.loads(raw_args or "{}") or {}).get("report"))
        except (TypeError, ValueError, AttributeError):
            return "error: `report` must be a report number from the index"
        if not 1 <= n <= len(reports):
            return f"error: there are {len(reports)} reports; ask for 1 to {len(reports)}"
        return f"RESEARCHER REPORT {n} of {len(reports)}:\n{reports[n - 1].strip()}"

    return specs, execute


__all__ = ["LANE_REPORT_BUDGET_CHARS", "PAGER_TOOL", "fits", "omitted",
           "render", "pager_spec", "with_pager"]
