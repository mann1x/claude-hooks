"""The comment at the cited line (docs/PLAN-council-memory.md, M3).

For four runs the critic ranked the read ledger "the biggest gap"; it is
unwired on purpose and says so in a comment above the line every report
cited, which no lane read. So every ``path:line`` a researcher report
cites reaches the critic and meta-critic with the comment block around
it, taken in code with no model call:

- the contiguous comment lines above the line (decorators skipped), the
  line itself when it is a comment or carries one;
- a docstring when the line is a Python definition.

A cite with no comment there adds nothing; a prompt whose cites have no
comments is unchanged.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger(__name__)

#: Comment lines kept above a cited line, nearest first.
BLOCK_MAX_LINES = 60
DOCSTRING_MAX_LINES = 60
#: Files read per call, and the largest one.
FILES_MAX = 400
FILE_MAX_BYTES = 4_000_000

_LINE_COMMENT = re.compile(r"^\s*(?://|#(?![!\w])|#\s|--\s|/\*|\*(?!\S)|\*/|\*\s|\"\"\"|''')")
_TRAILING = re.compile(r"\S.*?\s(?://|#)\s+\S")
_DECORATOR = re.compile(r"^\s*@\w")
_PY_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w")
_DOC_OPEN = re.compile(r"^\s*[rRbBuU]?(\"\"\"|''')")

HEADER = (
    "CODE COMMENTS AT CITED LINES (read from the tree as it is, verbatim; "
    "the comment block above each line the reports cite, and the "
    "docstring of a cited definition). A comment states what the author "
    "meant the code to do: a report that calls intended behaviour a "
    "defect must say why the comment is wrong, with a run.")


def _is_comment(line: str) -> bool:
    return bool(_LINE_COMMENT.match(line))


def comment_block(lines: list[str], n: int) -> Optional[tuple[int, int]]:
    """``(first, last)`` 1-based lines of the comment around line ``n``,
    the cited line included; None when there is none."""
    if not 1 <= n <= len(lines):
        return None
    first = last = n
    k = n - 1                                   # 1-based line above
    while k >= 1 and _DECORATOR.match(lines[k - 1]):
        k -= 1
    top = k
    while k >= 1 and _is_comment(lines[k - 1]) and top - k < BLOCK_MAX_LINES:
        k -= 1
    above = k < top
    if above:
        first = k + 1
    here = _is_comment(lines[n - 1]) or bool(_TRAILING.match(lines[n - 1].strip()))
    if here and _is_comment(lines[n - 1]):      # inside a comment: its end
        j = n
        while j < len(lines) and _is_comment(lines[j]) and j - n < BLOCK_MAX_LINES:
            j += 1
        last = j
    doc = _docstring(lines, n)
    if doc:
        last = max(last, doc[1])
    if not (above or here or doc):
        return None
    return first, last


def _docstring(lines: list[str], n: int) -> Optional[tuple[int, int]]:
    if not _PY_DEF.match(lines[n - 1]):
        return None
    j = n
    while j <= len(lines) and not lines[j - 1].rstrip().endswith(":") \
            and j - n < 20:
        j += 1                                  # a header over several lines
    j += 1
    while j <= len(lines) and not lines[j - 1].strip():
        j += 1
    if j > len(lines):
        return None
    m = _DOC_OPEN.match(lines[j - 1])
    if not m:
        return None
    q = m.group(1)
    rest = lines[j - 1].split(q, 1)[1]
    if q in rest:
        return j, j
    k = j + 1
    while k <= len(lines) and q not in lines[k - 1] and k - j < DOCSTRING_MAX_LINES:
        k += 1
    return j, min(k, len(lines))


def _resolve(path: str, roots: Sequence[str]) -> Optional[Path]:
    from consultants.engine.citation_linter import _resolve_path
    try:
        return _resolve_path(path, roots)
    except Exception:  # noqa: BLE001 — a cite never fails the critic
        return None


def blocks_for(reports: Sequence[str], roots: Sequence[str]) -> list[dict]:
    """One entry per distinct comment block the reports cite:
    ``{path, line, first, last, text, cites}``, most cited first."""
    from consultants.engine.typed_results import citations_of
    roots = [r for r in roots if r]
    if not roots:
        return []
    counts: Counter = Counter()
    for r in reports:
        for _m, path, start, _end in citations_of(r or ""):
            counts[(path, start)] += 1
    files: dict[str, Optional[list[str]]] = {}
    out: dict[tuple, dict] = {}
    for (path, n), c in counts.most_common():
        p = _resolve(path, roots)
        if p is None:
            continue
        key = str(p)
        if key not in files:
            if len(files) >= FILES_MAX:
                continue
            try:
                files[key] = (p.read_text(encoding="utf-8", errors="replace")
                              .split("\n") if p.stat().st_size <= FILE_MAX_BYTES
                              else None)
            except OSError:
                files[key] = None
        lines = files[key]
        if not lines:
            continue
        span = comment_block(lines, n)
        if span is None:
            continue
        first, last = span
        k = (key, first, last)
        if k in out:
            out[k]["cites"] += c
            continue
        body = "\n".join(f"{i:>6}  {lines[i - 1]}" for i in range(first, last + 1))
        out[k] = {"path": path, "line": n, "first": first, "last": last,
                  "text": body, "cites": c}
    return sorted(out.values(), key=lambda b: -b["cites"])


def block_text(blocks: list[dict], budget_chars: int) -> str:
    """The block for a prompt: whole comment blocks while they fit, the
    rest named. "" with none."""
    if not blocks:
        return ""
    out, used, left = [HEADER], len(HEADER), []
    for b in blocks:
        part = (f"\n`{b['path']}:{b['line']}` (cited {b['cites']}×), lines "
                f"{b['first']}–{b['last']}:\n```\n{b['text']}\n```")
        if used + len(part) > budget_chars and len(out) > 1:
            left.append(f"{b['path']}:{b['line']}")
            continue
        out.append(part)
        used += len(part)
    if left:
        out.append(f"\n(more cited lines carry comments not shown here: "
                   f"{', '.join(left)}; read them before judging those claims)")
    return "\n".join(out)


def with_cited_comments(msgs: list[dict], reports: Sequence[str],
                        roots: Sequence[str], budget_chars: int) -> list[dict]:
    """``msgs`` with the comment block appended to the last user turn;
    unchanged when no cited line has a comment. Never raises."""
    if not msgs or msgs[-1].get("role") != "user":
        return msgs
    try:
        text = block_text(blocks_for(reports, roots), budget_chars)
    except Exception:  # noqa: BLE001 — never fail the critic over this
        log.exception("cited comments unreadable")
        return msgs
    if not text:
        return msgs
    return msgs[:-1] + [{**msgs[-1], "content": (
        str(msgs[-1].get("content") or "") + "\n\n" + text)}]


__all__ = ["comment_block", "blocks_for", "block_text", "with_cited_comments",
           "HEADER"]
