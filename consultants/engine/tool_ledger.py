"""The numbered record of tool calls a compaction folds (plan C2, 02 P2-b).

Port of the fork's ``tool-ledger.ts`` (``/shared/dev/cline``) to the
OpenAI-shaped messages the council keeps: an assistant turn's
``tool_calls[{id, function{name, arguments}}]`` answered by ``role: tool``
messages carrying ``tool_call_id``.

Why it exists. A replay writer asked to narrate its work copies its tool
calls out, and the fork measured that transcription taking 69% of a small
model's output while the prose was cut mid-sentence. So the writer is
given the *key* (``N. tool  input``) and cites ``[#N]``; after the review
the harness splices each cited entry in where the replay put it, and
appends the uncited ones after the prose. The model's account and the
measured record sit side by side, so a disagreement is visible rather
than silent.

Rules carried over:

* calls and results are paired **by id, never by position**;
* an identical ``(tool, input, result)`` repeat collapses into ``N×``; a
  changed result is kept, because it is the informative pair;
* fields 200 chars, input 400, result 300; a string of four lines or more
  is reported as its size;
* a refusal hidden in a success envelope (``"ok": false``,
  ``"error": "…"``, a leading error/refused/failed) is marked FAILED;
* eviction to budget loses what can be redone first: successful
  non-verdict calls, then failed ones, then verdicts. Our verdict tools
  are ``run_*`` / ``check_*``, and a ``grep`` that found nothing — what a
  checker or an empty search said cannot be recovered from anywhere;
* a citation to a missing, repeated, backwards or over-wide entry is
  dropped and counted as ``invalid``: the only signal that the prose ran
  past the record.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

MAX_FIELD_CHARS = 200
MAX_INPUT_CHARS = 400
MAX_RESULT_CHARS = 300
MULTILINE_LINE_FLOOR = 4

#: The ledger's share of the compaction target, in characters at three
#: per token, with a floor of roughly a dozen entries.
LEDGER_BUDGET_SHARE = 0.10
MIN_LEDGER_CHARS = 2_000

_NAME_KEYS = ("command", "cmd", "path", "file_path", "filePath", "query",
              "pattern", "url", "name", "text")
_PATH_KEYS = ("path", "file", "file_path", "filePath", "target")


@dataclass
class Entry:
    index: int
    tool: str
    input: str
    result: str
    failed: bool = False
    repeated: int = 1
    files: list[str] = field(default_factory=list)
    verdict: bool = False

    def as_dict(self) -> dict:
        return {"index": self.index, "tool": self.tool, "input": self.input,
                "result": self.result, "failed": self.failed,
                "repeated": self.repeated, "files": list(self.files),
                "verdict": self.verdict}

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(index=int(d.get("index") or 0), tool=str(d.get("tool") or ""),
                   input=str(d.get("input") or ""),
                   result=str(d.get("result") or ""),
                   failed=bool(d.get("failed")),
                   repeated=int(d.get("repeated") or 1),
                   files=list(d.get("files") or []),
                   verdict=bool(d.get("verdict")))


def _elide(text: str, limit: int) -> str:
    keep = max(20, limit // 2 - 12)
    return f"{text[:keep]} …{len(text) - keep * 2} chars elided… {text[-keep:]}"


def _value(value: Any) -> str:
    """One field, short enough to read; a multi-line body as its shape."""
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        return _list(value)
    if isinstance(value, dict):
        return _entry_name(value) or "{" + ", ".join(value) + "}"
    text = str(value)
    lines = text.split("\n")
    if len(lines) >= MULTILINE_LINE_FLOOR:
        return f"<{len(lines)} lines, {len(text)} chars>"
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return _elide(text, MAX_FIELD_CHARS)


def _entry_name(entry: dict) -> Optional[str]:
    for key in _NAME_KEYS:
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            continue
        if key in ("command", "cmd"):
            args = [a for a in (entry.get("args") or []) if isinstance(a, str)]
            return _value(f"{value} {' '.join(args)}" if args else value)
        start, end = entry.get("start_line"), entry.get("end_line")
        if isinstance(start, int) and isinstance(end, int):
            return _value(f"{value}:{start}-{end}")
        return _value(value)
    return None


def _list(value: list) -> str:
    if not value:
        return "[]"
    parts: list[str] = []
    used = 0
    for i, item in enumerate(value):
        part = (f"[{len(item)} items]" if isinstance(item, list)
                else (_entry_name(item) or "{" + ", ".join(item) + "}")
                if isinstance(item, dict) else _value(item))
        if parts and used + len(part) > MAX_FIELD_CHARS:
            parts.append(f"+{len(value) - i} more")
            break
        parts.append(part)
        used += len(part) + 2
    return parts[0] if len(parts) == 1 else "[" + ", ".join(parts) + "]"


def _clamp(text: str, limit: int) -> str:
    flat = re.sub(r"\s*\n\s*", " ⏎ ", text).strip()
    return flat if len(flat) <= limit else _elide(flat, limit)


def _arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def summarize_input(raw: Any) -> str:
    args = _arguments(raw)
    if not isinstance(args, dict):
        return _clamp(_value(args), MAX_INPUT_CHARS)
    return _clamp(" ".join(f"{k}={_value(v)}" for k, v in args.items()),
                  MAX_INPUT_CHARS)


def summarize_output(content: Any) -> str:
    if isinstance(content, list):
        content = " ".join(str(b.get("text") or "") for b in content
                           if isinstance(b, dict))
    return _clamp(str(content or ""), MAX_RESULT_CHARS)


def looks_refused(content: Any, is_error: bool = False) -> bool:
    if is_error:
        return True
    text = content if isinstance(content, str) else json.dumps(content,
                                                                 default=str)
    if not text:
        return False
    return bool(re.search(r'"ok"\s*:\s*false', text, re.I)
                or re.search(r'"(?:error|failure)"\s*:\s*"', text, re.I)
                or re.match(r"\s*(?:error|refused|failed)\b", text, re.I))


def _no_matches(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return (not lowered or lowered.startswith("no match")
            or "no matches" in lowered[:80] or lowered in ("[]", "0 matches"))


def is_verdict(tool: str, result_text: str = "") -> bool:
    """A call whose result is a judgement rather than a copy of something
    still on disk: ``run_*`` / ``check_*``, and a search that found
    nothing (an empty grep is the evidence an absence rests on)."""
    name = (tool or "").lower()
    if name.startswith("run_") or name.startswith("check_"):
        return True
    return name == "grep" and _no_matches(result_text)


def _paths(raw: Any) -> list[str]:
    args = _arguments(raw)
    if not isinstance(args, dict):
        return []
    found = [str(args[k]) for k in _PATH_KEYS
             if isinstance(args.get(k), str) and args.get(k)]
    for item in args.get("files") or args.get("paths") or []:
        if isinstance(item, str) and item:
            found.append(item)
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            found.append(item["path"])
    return list(dict.fromkeys(found))


def build(messages: list[dict]) -> list[Entry]:
    """The calls in a stretch of messages, each paired with its result by
    ``tool_call_id``, identical repeats collapsed."""
    results: dict[str, dict] = {}
    for m in messages or []:
        if (m or {}).get("role") == "tool" and m.get("tool_call_id"):
            results[m["tool_call_id"]] = m
    entries: list[Entry] = []
    for m in messages or []:
        if (m or {}).get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "(unnamed)")
            answer = results.get((tc or {}).get("id") or "")
            inp = summarize_input(fn.get("arguments"))
            if answer is None:
                result, failed, raw = "(no result recorded)", False, ""
            else:
                raw = answer.get("content")
                result = summarize_output(raw)
                failed = looks_refused(raw, bool(answer.get("is_error")))
            prev = entries[-1] if entries else None
            if (prev and prev.tool == name and prev.input == inp
                    and prev.result == result):
                prev.repeated += 1
                continue
            entries.append(Entry(
                index=len(entries) + 1, tool=name, input=inp, result=result,
                failed=failed, files=_paths(fn.get("arguments")),
                verdict=is_verdict(name, raw if isinstance(raw, str) else "")))
    return entries


def _renumber(entries: list[Entry]) -> list[Entry]:
    return [replace(e, index=i + 1) for i, e in enumerate(entries)]


def merge(previous: list[Entry], new: list[Entry]) -> list[Entry]:
    """A carried ledger and this compaction's, as one run, with the repeat
    rule applied across the seam."""
    merged: list[Entry] = []
    for e in list(previous) + list(new):
        last = merged[-1] if merged else None
        if (last and last.tool == e.tool and last.input == e.input
                and last.result == e.result):
            last.repeated += e.repeated
            continue
        merged.append(replace(e, files=list(e.files)))
    return _renumber(merged)


def _chars(e: Entry) -> int:
    return len(e.tool) + len(e.input) + len(e.result) + 16


def budget_chars(target_tokens: int) -> int:
    return max(MIN_LEDGER_CHARS, int(target_tokens * 3 * LEDGER_BUDGET_SHARE))


def evicted(entries: list[Entry], budget: int) -> set[int]:
    """The positions :func:`evict` drops to bring ``entries`` under
    ``budget`` characters: what can be redone first, oldest first within
    a tier (successful calls, then failed ones, then verdicts)."""
    total = sum(_chars(e) for e in entries)

    def tier(e: Entry) -> int:
        return 2 if e.verdict else (1 if e.failed else 0)

    order = sorted(range(len(entries)), key=lambda i: (tier(entries[i]), i))
    dropped: set[int] = set()
    for i in order:
        if total <= budget:
            break
        dropped.add(i)
        total -= _chars(entries[i])
    return dropped


def evict(entries: list[Entry], budget: int) -> list[Entry]:
    """Bring the ledger under ``budget`` characters. Whole entries only:
    half an entry says something that did not happen."""
    dropped = evicted(entries, budget)
    return _renumber([e for i, e in enumerate(entries) if i not in dropped])


def render_entry(e: Entry) -> str:
    marks = (["FAILED"] if e.failed else []) + (
        [f"{e.repeated}×"] if e.repeated > 1 else [])
    mark = f" [{' '.join(marks)}]" if marks else ""
    return f"{e.index}. {e.tool}{mark}  {e.input}\n      → {e.result}"


def render(entries: list[Entry]) -> str:
    return "\n".join(render_entry(e) for e in entries)


def render_key(entries: list[Entry]) -> str:
    """What the writer cites against: which call is which, and nothing
    else — it has the conversation for the results."""
    return "\n".join(f"{e.index}. {e.tool}  {e.input}" for e in entries)


_CITATION = re.compile(r"\[#(\d+)(?:\s*[-–—]\s*#?(\d+))?\]")


@dataclass
class Splice:
    text: str
    cited: list[int]
    uncited: list[int]
    invalid: list[int]


def splice(replay: str, entries: list[Entry]) -> Splice:
    """Put each cited call where the replay says it happened."""
    by_index = {e.index: e for e in entries}
    used: set[int] = set()
    cited: list[int] = []
    invalid: list[int] = []

    def place(i: int) -> Optional[str]:
        e = by_index.get(i)
        if e is None or i in used:
            return None
        used.add(i)
        cited.append(i)
        return render_entry(e)

    def sub(m: re.Match) -> str:
        start = int(m.group(1))
        if m.group(2) is None:
            placed = place(start)
            if placed is None:
                invalid.append(start)
                return ""
            return f"\n{placed}\n"
        end = int(m.group(2))
        if end < start or end - start + 1 > len(entries):
            invalid.append(start)
            return ""
        out = []
        for i in range(start, end + 1):
            placed = place(i)
            if placed is None:
                invalid.append(i)
            else:
                out.append(placed)
        return f"\n{chr(10).join(out)}\n" if out else ""

    text = _CITATION.sub(sub, replay or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Splice(text=text, cited=cited,
                  uncited=[e.index for e in entries if e.index not in used],
                  invalid=invalid)


__all__ = ["Entry", "Splice", "build", "merge", "evict", "evicted", "budget_chars",
           "render", "render_entry", "render_key", "splice", "is_verdict",
           "looks_refused", "summarize_input", "summarize_output"]
