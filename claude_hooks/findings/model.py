"""A finding, and its file format (docs/PLAN-council-memory.md).

A finding is a claim with a verdict: what a council said about the code,
the evidence for it, and — once someone decides — whether it holds. The
council records candidates; only the user or the assistant reviewing the
council settles them (:func:`claude_hooks.findings.ledger.Ledger.settle`).

Same file shape as a task (``claude_hooks.tasks.model``): front matter in
the restricted YAML subset the tasks parser reads, then sections. The
canonical sections are ``Claim``, ``Reproduce``, ``Evidence`` (newest
first), ``Decision`` and ``Log`` (newest first); any other a person adds
is kept.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from claude_hooks.tasks.model import (
    TaskFormatError, parse_value, render_value, stamp, utcnow,
)

CANDIDATE = "candidate"
CONFIRMED = "confirmed"
REFUTED = "refuted"
BY_DESIGN = "by_design"
FIXED = "fixed"
STATUSES = (CANDIDATE, CONFIRMED, REFUTED, BY_DESIGN, FIXED)
SETTLED = (CONFIRMED, REFUTED, BY_DESIGN, FIXED)
SEVERITIES = ("H", "M", "L")
#: Who may settle a finding: the user, or any assistant session (``by:
#: assistant``), including one driving the council. The council is not.
DECIDERS = ("user", "assistant")

CLAIM = "Claim"
REPRODUCE = "Reproduce"
EVIDENCE = "Evidence"
DECISION = "Decision"
LOG = "Log"
_ORDER = (CLAIM, REPRODUCE, EVIDENCE, DECISION, LOG)

FIELDS = ("id", "title", "status", "severity", "paths", "symbols",
          "councils", "task", "commit", "settled_by", "settled_at",
          "created", "updated")
LIST_FIELDS = ("paths", "symbols", "councils")
_ALWAYS = ("id", "title", "status", "severity", "created", "updated")

_ID = re.compile(r"^[a-z][a-z0-9]*-\d+$")
_HEADING = re.compile(r"^## +(.+?)\s*$")

_ALIASES = {
    "new": CANDIDATE, "open": CANDIDATE, "unconfirmed": CANDIDATE,
    "real": CONFIRMED, "valid": CONFIRMED, "true": CONFIRMED,
    "false": REFUTED, "invalid": REFUTED, "wrong": REFUTED,
    "by-design": BY_DESIGN, "bydesign": BY_DESIGN, "deliberate": BY_DESIGN,
    "intended": BY_DESIGN, "wontfix": BY_DESIGN, "resolved": FIXED,
}


def normalise_status(value: str, *, strict: bool = True) -> str:
    s = str(value or "").strip().lower().replace(" ", "_")
    if s in STATUSES:
        return s
    if s in _ALIASES:
        return _ALIASES[s]
    if strict:
        raise ValueError(f"unknown status {value!r}; one of {', '.join(STATUSES)}")
    return CANDIDATE


def is_finding_id(text: str) -> bool:
    return bool(_ID.match(str(text or "").strip().lower()))


@dataclass
class Finding:
    id: str
    title: str
    status: str = CANDIDATE
    severity: str = "M"
    paths: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    councils: list = field(default_factory=list)
    task: str = ""
    commit: str = ""
    settled_by: str = ""
    settled_at: str = ""
    created: str = ""
    updated: str = ""
    sections: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # sections ------------------------------------------------------

    def section(self, name: str) -> str:
        for h, text in self.sections:
            if h.lower() == name.lower():
                return text
        return ""

    def set_section(self, name: str, text: str) -> None:
        text = "\n".join("#" + ln if _HEADING.match(ln) else ln
                         for ln in (text or "").strip("\n").split("\n"))
        for i, (h, _) in enumerate(self.sections):
            if h.lower() == name.lower():
                self.sections[i] = (h, text)
                return
        if name in _ORDER:
            later = _ORDER[_ORDER.index(name) + 1:]
            for i, (h, _) in enumerate(self.sections):
                if h in later:
                    self.sections.insert(i, (name, text))
                    return
        self.sections.append((name, text))

    def _prepend(self, name: str, line: str) -> None:
        old = self.section(name)
        self.set_section(name, line + ("\n" + old if old else ""))

    @property
    def claim(self) -> str:
        return self.section(CLAIM)

    @property
    def reproduce(self) -> str:
        return self.section(REPRODUCE)

    @property
    def decision(self) -> str:
        return self.section(DECISION)

    @property
    def evidence_lines(self) -> list[str]:
        return [ln for ln in self.section(EVIDENCE).splitlines()
                if ln.strip().startswith("- ")]

    def add_evidence(self, text: str, *, council: str = "",
                     when: Optional[datetime] = None) -> str:
        when = when or utcnow()
        body = " ".join(str(text).split())
        sid = f" {council}" if council else ""
        line = f"- {when.astimezone(timezone.utc):%Y-%m-%d}{sid}: {body}"
        # Council run 8: one run, recorded three times from one answer.
        same = next((ln for ln in self.evidence_lines
                     if ln.endswith(f"{sid}: {body}")), None)
        if same is not None:
            return same
        self._prepend(EVIDENCE, line)
        if council and council not in self.councils:
            self.councils.append(council)
        return line

    def add_log(self, text: str, *, when: Optional[datetime] = None) -> str:
        when = when or utcnow()
        line = (f"- {when.astimezone(timezone.utc):%Y-%m-%d %H:%M}Z "
                + " ".join(str(text).split()))
        self._prepend(LOG, line)
        return line

    # state ---------------------------------------------------------

    @property
    def settled(self) -> bool:
        return self.status in SETTLED

    def touch(self, when: Optional[datetime] = None) -> None:
        self.updated = stamp(when)

    # io ------------------------------------------------------------

    def to_markdown(self) -> str:
        lines = ["---"]
        for name in FIELDS:
            value = getattr(self, name)
            if name not in _ALWAYS and value in ("", [], {}):
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
    def from_markdown(cls, text: str, *, source: str = "") -> "Finding":
        where = f" in {source}" if source else ""
        text = text.replace("\r\n", "\n").lstrip("﻿")
        if not text.startswith("---\n"):
            raise TaskFormatError(f"no front matter{where}")
        end = text.find("\n---", 4)
        if end < 0:
            raise TaskFormatError(f"front matter is not closed{where}")
        meta: dict[str, Any] = {}
        for ln in text[4:end].split("\n"):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if ":" not in ln:
                raise TaskFormatError(f"not a 'key: value' line{where}: {ln!r}")
            k, _, v = ln.partition(":")
            meta[k.strip()] = parse_value(v)
        if not meta.get("id"):
            raise TaskFormatError(f"no id{where}")
        rest = text[end + 4:]
        rest = rest[1:] if rest.startswith("\n") else rest
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
                current, buf = CLAIM, [ln]
        if current is not None:
            sections.append((current, "\n".join(buf).strip("\n")))
        kwargs: dict = {"sections": sections}
        extra: dict = {}
        for k, v in meta.items():
            if k not in FIELDS:
                extra[k] = v
                continue
            if k in LIST_FIELDS:
                v = v if isinstance(v, list) else ([v] if v else [])
                v = [str(x) for x in v]
            elif v is None:
                v = ""
            elif not isinstance(v, str):
                v = str(v)
            kwargs[k] = v
        kwargs["id"] = str(kwargs["id"]).strip().lower()
        kwargs.setdefault("title", "")
        f = cls(extra=extra, **kwargs)
        f.status = normalise_status(f.status, strict=False)
        f.severity = (f.severity or "M").upper()[:1]
        if f.severity not in SEVERITIES:
            f.severity = "M"
        return f


__all__ = ["Finding", "STATUSES", "SETTLED", "SEVERITIES", "DECIDERS",
           "CANDIDATE", "CONFIRMED", "REFUTED", "BY_DESIGN", "FIXED",
           "normalise_status", "is_finding_id"]
