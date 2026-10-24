"""Cut digest lines that state facts about files no call opened.

compaction-v7-g2 / -cite (2026-10-24): on every first fold, in both
flows, the writer or a half-critic completed the module table the task
asks for — fields for 66–118 modules the lane had not read yet, one bullet
each, uncited, and essentially none right (1 of 98). The replay is the
lane's only record of the fold, so a lane that trusts it answers with
invented facts. Deterministic: the record knows which files each call
put in front of the model, so a line whose every file is one no call
opened says nothing the lane read, whatever it claims.

A file counts as **seen** when a call that shows content named it — in
its arguments (``read_file`` path, a ``run_command`` that cats it) or in
its result (a ``grep`` match line). Listing calls (``list_files``,
``glob``, ``survey_project``) prove a file exists, not what it holds, so
they never make one seen. A file named in the task or a typed request
is never cut either: an instruction that names it is not an invention.
The names the cut lines mentioned are kept, as one note, so a legitimate
"still to read" list survives as names.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

#: Tools whose results name files without showing what they contain.
LISTING_TOOLS = frozenset({"list_files", "glob", "survey_project",
                           "list_dir", "list_directory"})

#: The most names the note lists; the rest are counted.
NOTE_NAMES = 60

#: The most seen files carried to the next generation.
MAX_CARRIED = 5000

#: A file path: segments of word characters, dots and dashes, ending in
#: an extension of lower-case letters or digits. ``limits.RETRY_WINDOW``
#: (upper case) and ``1.5`` (no letter) are not files.
_FILE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?=[a-z0-9]*[a-z])[a-z0-9]{1,6})"
    r"(?![\w/-])")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
#: A name with no directory is a file only with one of these: without the
#: list, ``os.path.join`` or ``self.store.ack`` would read as files.
EXTENSIONS = frozenset("""
    py pyi ts tsx js jsx mjs cjs go rs c h cc cpp cxx hpp hh cs java kt kts
    rb php sh bash zsh ps1 bat cmd md rst txt json jsonl yaml yml toml ini
    cfg conf xml html htm css scss sql proto lua swift mm scala ex exs erl
    hs ml vue svelte dart jl zig nim tf gradle lock env log csv tsv ipynb
    """.split())

NOTE_HEAD = "Not yet read"


def files_in(text: str) -> list[str]:
    """The file paths ``text`` names, in order, without duplicates."""
    found = _FILE.findall(_URL.sub(" ", text or ""))
    return list(dict.fromkeys(
        f.lstrip("./") for f in found
        if "/" in f or f.rsplit(".", 1)[1] in EXTENSIONS))


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _content(message: dict) -> str:
    c = message.get("content")
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and isinstance(p.get("text"), str))
    return str(c or "")


def seen_in(messages: list[dict]) -> set[str]:
    """Files a call put in front of the model, in arguments or results."""
    listing: set[str] = set()
    seen: set[str] = set()
    for m in messages or []:
        for tc in (m or {}).get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            if str(fn.get("name") or "") in LISTING_TOOLS:
                if tc.get("id"):
                    listing.add(tc["id"])
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            for s in _strings(args):
                seen.update(files_in(s))
    for m in messages or []:
        if (m or {}).get("role") == "tool" and m.get("tool_call_id") not in listing:
            seen.update(files_in(_content(m)))
    return seen


def _known(name: str, seen: set[str]) -> bool:
    """``name`` is a seen file, or a path suffix of one (``net/backoff.py``
    for ``src/net/backoff.py``), or one is a suffix of it."""
    if name in seen:
        return True
    for s in seen:
        if s.endswith("/" + name) or name.endswith("/" + s):
            return True
    return False


def drop_unread(text: str, seen: set[str], *,
                exempt: str = "") -> tuple[str, list[str], int]:
    """``text`` without the lines whose every named file is unseen; the
    names those lines mentioned, and how many lines went. ``exempt`` is
    text (the task, the typed requests) whose files are never cut."""
    allowed = set(seen) | set(files_in(exempt))
    out: list[str] = []
    names: list[str] = []
    cut = 0
    fence = False
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        named = [] if fence else files_in(line)
        if named and not any(_known(n, allowed) for n in named):
            cut += 1
            names += [n for n in named if n not in names]
            continue
        out.append(line)
    return "\n".join(out), names, cut


def note(names: list[str], cut: int) -> str:
    """What the cut leaves in the digest: the names, never the claims."""
    shown = names[:NOTE_NAMES]
    more = len(names) - len(shown)
    return (f"{NOTE_HEAD} ({cut} line(s) about these files were removed from "
            f"this summary: no call in the record opened them, so what the "
            f"lines said about them was not read from the files): "
            + ", ".join(f"`{n}`" for n in shown)
            + (f", and {more} more" if more > 0 else "") + ".")


def carried(previous: Iterable[str], new: Iterable[str]) -> list[str]:
    """The seen set for the marker, newest kept when over the bound."""
    out = list(dict.fromkeys([*previous, *sorted(new)]))
    return out[-MAX_CARRIED:]


__all__ = ["EXTENSIONS", "LISTING_TOOLS", "NOTE_HEAD", "files_in", "seen_in",
           "drop_unread", "note", "carried"]
