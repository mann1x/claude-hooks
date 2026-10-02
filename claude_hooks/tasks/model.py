"""A task, and its file format.

One Markdown file per task: front matter for the fields the tools
change, then ``## Description`` and ``## Acceptance`` (rewritten in
place) and ``## Log`` (append-only, newest first). Any other section a
person adds is kept, in its place.

The front matter is a restricted YAML subset — bare or double-quoted
scalars, flow lists ``[a, b]`` and flow maps ``{k: [a]}`` — parsed with
the stdlib, because the core stays dependency-free and because a format
we write ourselves only needs to read back what we write plus what a
person plausibly types over it. Anything it does not understand is kept
as a string rather than refused: a hand edit that half-parses must not
lose the task.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

STATUSES = ("pending", "active", "waiting", "done", "cancelled")
OPEN_STATUSES = ("pending", "active", "waiting")
CLOSED_STATUSES = ("done", "cancelled")
PRIORITIES = ("H", "M", "L")

DESCRIPTION = "Description"
ACCEPTANCE = "Acceptance"
LOG = "Log"

#: Field order in the file. A stable order keeps diffs to the line that
#: changed, which is what makes the files reviewable in git.
FIELDS = ("id", "title", "status", "priority", "area", "tags", "depends",
          "plan", "due", "links", "created", "updated", "sessions")
LIST_FIELDS = ("tags", "depends", "sessions")

_ID = re.compile(r"^([a-z][a-z0-9]*)-(\d+)$")
_BARE = re.compile(r"^[A-Za-z0-9_./@+:-]+$")
_HEADING = re.compile(r"^## +(.+?)\s*$")


class TaskFormatError(ValueError):
    """A task file that cannot be read back; the message says where."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(dt: Optional[datetime] = None) -> str:
    """Front-matter time: ISO-8601 UTC, seconds, ``Z``."""
    return (dt or utcnow()).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def split_id(task_id: str) -> tuple[str, int]:
    """``bm-42`` → ``("bm", 42)``."""
    m = _ID.match(str(task_id).strip().lower())
    if not m:
        raise ValueError(f"not a task id: {task_id!r} (expected e.g. bm-42)")
    return m.group(1), int(m.group(2))


def is_task_id(text: str) -> bool:
    return bool(_ID.match(str(text).strip().lower()))


# ─── front-matter values ─────────────────────────────────────────────


def _render_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s and _BARE.match(s) and s.lower() not in ("true", "false", "null"):
        return s
    return json.dumps(s, ensure_ascii=False)


def render_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {render_value(v)}"
                               for k, v in value.items()) + "}"
    return _render_scalar(value)


class _Flow:
    """Tokenizer for one flow value: ``[a, "b c"]``, ``{k: [1]}``."""

    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def value(self) -> Any:
        self.ws()
        if self.i >= len(self.s):
            return ""
        c = self.s[self.i]
        if c == "[":
            return self.seq()
        if c == "{":
            return self.mapping()
        if c == '"':
            return self.quoted()
        return self.bare(",]}")

    def quoted(self) -> str:
        start = self.i
        self.i += 1
        while self.i < len(self.s):
            if self.s[self.i] == "\\":
                self.i += 2
                continue
            if self.s[self.i] == '"':
                self.i += 1
                return json.loads(self.s[start:self.i])
            self.i += 1
        raise TaskFormatError(f"unterminated string in {self.s!r}")

    def bare(self, stops: str) -> str:
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in stops:
            self.i += 1
        return self.s[start:self.i].strip()

    def seq(self) -> list:
        self.i += 1
        out: list = []
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise TaskFormatError(f"unclosed [ in {self.s!r}")
            if self.s[self.i] == "]":
                self.i += 1
                return out
            item = self.value()
            if item != "":
                out.append(item)
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1

    def mapping(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise TaskFormatError(f"unclosed {{ in {self.s!r}")
            if self.s[self.i] == "}":
                self.i += 1
                return out
            key = self.quoted() if self.s[self.i] == '"' else self.bare(":}")
            if self.i < len(self.s) and self.s[self.i] == ":":
                self.i += 1
            out[key] = self.value()
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1


def _strip_comment(raw: str) -> str:
    """Drop a trailing ``# comment`` that sits outside any string."""
    in_str = False
    esc = False
    for i, c in enumerate(raw):
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
        elif c == '"':
            in_str = not in_str
        elif c == "#" and not in_str and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i].rstrip()
    return raw.strip()


def parse_value(raw: str) -> Any:
    raw = _strip_comment(raw)
    if raw == "":
        return ""
    if raw[0] in '[{"':
        try:
            p = _Flow(raw)
            v = p.value()
            return v
        except (TaskFormatError, ValueError):
            return raw          # keep what the person typed
    if raw in ("true", "false"):
        return raw == "true"
    if raw == "null":
        return None
    return raw


# ─── the task ─────────────────────────────────────────────────────────


@dataclass
class Task:
    id: str
    title: str
    status: str = "pending"
    priority: str = "M"
    area: str = ""
    tags: list = field(default_factory=list)
    depends: list = field(default_factory=list)
    plan: str = ""
    due: str = ""
    links: dict = field(default_factory=dict)
    created: str = ""
    updated: str = ""
    sessions: list = field(default_factory=list)
    #: ``[(heading, text)]`` in file order; Description / Acceptance /
    #: Log are three of them, anything else a person added is kept.
    sections: list = field(default_factory=list)
    #: Front-matter keys we do not model, kept so a round trip loses nothing.
    extra: dict = field(default_factory=dict)

    # sections ------------------------------------------------------

    def section(self, name: str) -> str:
        for h, text in self.sections:
            if h.lower() == name.lower():
                return text
        return ""

    def set_section(self, name: str, text: str) -> None:
        text = (text or "").strip("\n")
        for i, (h, _) in enumerate(self.sections):
            if h.lower() == name.lower():
                self.sections[i] = (h, text)
                return
        order = [DESCRIPTION, ACCEPTANCE, LOG]
        if name in order:
            # Keep the canonical three in their order, ahead of extras.
            later = order[order.index(name) + 1:]
            for i, (h, _) in enumerate(self.sections):
                if h in later:
                    self.sections.insert(i, (name, text))
                    return
        self.sections.append((name, text))

    @property
    def description(self) -> str:
        return self.section(DESCRIPTION)

    @property
    def acceptance(self) -> str:
        return self.section(ACCEPTANCE)

    @property
    def log_lines(self) -> list[str]:
        return [ln for ln in self.section(LOG).splitlines()
                if ln.strip().startswith("- ")]

    def add_log(self, text: str, *, session: str = "",
                when: Optional[datetime] = None) -> str:
        """Prepend one log line (newest first); return it."""
        when = when or utcnow()
        sid = f" [{session[:8]}]" if session else ""
        body = " ".join(str(text).split())
        line = f"- {when.astimezone(timezone.utc):%Y-%m-%d %H:%M}Z{sid} {body}"
        old = self.section(LOG)
        self.set_section(LOG, line + ("\n" + old if old else ""))
        return line

    def acceptance_counts(self) -> tuple[int, int]:
        """(checked, total) acceptance boxes."""
        done = total = 0
        for ln in self.acceptance.splitlines():
            s = ln.strip().lower()
            if s.startswith("- [x]"):
                done += 1
                total += 1
            elif s.startswith("- [ ]"):
                total += 1
        return done, total

    # state ---------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def number(self) -> int:
        return split_id(self.id)[1]

    @property
    def prefix(self) -> str:
        return split_id(self.id)[0]

    def touch(self, session: str = "", when: Optional[datetime] = None) -> None:
        self.updated = stamp(when)
        if session:
            short = session[:8]
            if short not in self.sessions:
                self.sessions.append(short)

    # io ------------------------------------------------------------

    def to_markdown(self) -> str:
        lines = ["---"]
        for name in FIELDS:
            value = getattr(self, name)
            if name not in ("id", "title", "status", "priority",
                            "created", "updated") and value in ("", [], {}):
                continue
            lines.append(f"{name}: {render_value(value)}")
        for k, v in self.extra.items():
            lines.append(f"{k}: {render_value(v)}")
        lines.append("---")
        out = "\n".join(lines) + "\n"
        for heading, text in self.sections:
            out += f"\n## {heading}\n"
            if text:
                out += text.rstrip("\n") + "\n"
        return out

    @classmethod
    def from_markdown(cls, text: str, *, source: str = "") -> "Task":
        where = f" in {source}" if source else ""
        text = text.replace("\r\n", "\n").lstrip("﻿")
        if not text.startswith("---\n"):
            raise TaskFormatError(f"no front matter{where}")
        end = text.find("\n---", 4)
        if end < 0:
            raise TaskFormatError(f"front matter is not closed{where}")
        head = text[4:end]
        rest = text[end + 4:]
        rest = rest[1:] if rest.startswith("\n") else rest
        meta: dict = {}
        for ln in head.split("\n"):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if ":" not in ln:
                raise TaskFormatError(f"not a 'key: value' line{where}: {ln!r}")
            k, _, v = ln.partition(":")
            meta[k.strip()] = parse_value(v)
        if not meta.get("id"):
            raise TaskFormatError(f"no id{where}")
        sections: list = []
        current: Optional[str] = None
        buf: list[str] = []
        for ln in rest.split("\n"):
            m = _HEADING.match(ln)
            if m:
                if current is not None:
                    sections.append((current, "\n".join(buf).strip("\n")))
                current, buf = m.group(1), []
            elif current is not None:
                buf.append(ln)
            elif ln.strip():
                current, buf = DESCRIPTION, [ln]
        if current is not None:
            sections.append((current, "\n".join(buf).strip("\n")))
        kwargs: dict = {"sections": sections}
        extra: dict = {}
        for k, v in meta.items():
            if k in FIELDS:
                if k in LIST_FIELDS:
                    v = v if isinstance(v, list) else ([v] if v else [])
                    v = [str(x) for x in v]
                elif k == "links":
                    v = v if isinstance(v, dict) else {}
                elif v is None:
                    v = ""
                elif not isinstance(v, str):
                    v = str(v)
                kwargs[k] = v
            else:
                extra[k] = v
        kwargs["id"] = str(kwargs["id"]).strip().lower()
        kwargs.setdefault("title", "")
        task = cls(extra=extra, **kwargs)
        if task.status not in STATUSES:
            task.status = _normalise_status(task.status)
        task.priority = (task.priority or "M").upper()[:1]
        if task.priority not in PRIORITIES:
            task.priority = "M"
        return task


_STATUS_ALIASES = {
    "todo": "pending", "open": "pending", "ready": "pending",
    "in_progress": "active", "in-progress": "active", "doing": "active",
    "started": "active", "blocked": "waiting", "wait": "waiting",
    "completed": "done", "closed": "done", "finished": "done",
    "deleted": "cancelled", "canceled": "cancelled", "dropped": "cancelled",
}


def _normalise_status(value: str) -> str:
    """A hand-typed status, mapped onto the five we keep."""
    s = str(value or "").strip().lower()
    return s if s in STATUSES else _STATUS_ALIASES.get(s, "pending")


def normalise_status(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in STATUSES:
        return s
    if s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    raise ValueError(f"unknown status {value!r}; one of {', '.join(STATUSES)}")
