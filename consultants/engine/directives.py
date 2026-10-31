"""Operator directives: an inject binds the final answer.

An inject used to reach the synthesizer as context it could weigh, and
nothing compared the answer with it. A follow-up told not to attribute a
rise to rounding before the control result still called it an instrument
artefact (csl-2026-10-31-1051-a38d). Now an auditor, thinking off, holds the
answer to each directive: a violation gets one re-ask, and what the delivered
answer still does not follow is stated under :data:`HEADING`, never hidden.

Directives are instructions. A claim of fact inside one is not made true by
being injected; :data:`consultants.engine.evidence.BRIEF_ATTRIBUTION` still
governs how the answer credits it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Injected docs that are engine-built context, not the operator's orders.
NOT_DIRECTIVES = frozenset({"already_tried"})

BINDING_NOTE = (
    "These come from the operator and bind you. Do what each one tells you "
    "and keep every constraint it sets. If you cannot, because it conflicts "
    "with the evidence or with another directive, do not depart from it "
    "silently: say so in your output, naming the directive and the reason.")

HEADING = "## Operator directives not met"

AUDIT_SYSTEM = (
    "You check a final answer against the operator's directives. Judge only "
    "what a directive instructs or forbids, including what it forbids until "
    "a condition holds. Facts a directive states are background, not "
    "instructions. An answer that openly declines an instruction and gives "
    "its reason has met it. An answer that breaks one in passing, in a "
    "single clause, has not.\n"
    "Reply with exactly one line per directive and nothing else:\n"
    "N MET\n"
    "N VIOLATED: <the instruction broken> -- \"<the answer's words>\"")

_VERDICT = re.compile(r"^\W*(\d+)\W+(MET|VIOLATED)\b\W*(.*)$", re.I)


def directives_of(docs) -> list:
    """The docs that bind: injected by an operator, with text."""
    return [d for d in docs or ()
            if getattr(d, "source", "user") not in NOT_DIRECTIVES
            and (getattr(d, "text", "") or "").strip()]


def audit_prompt(docs, answer: str) -> str:
    listed = "\n\n".join(f"DIRECTIVE {i}:\n{d.text.strip()}"
                         for i, d in enumerate(docs, start=1))
    return f"{listed}\n\nFINAL ANSWER:\n{answer}"


@dataclass
class Audit:
    """``violated``: (directive number, what was broken). ``unchecked``:
    numbers the auditor gave no verdict on."""
    violated: list = field(default_factory=list)
    unchecked: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violated and not self.unchecked


def parse_audit(reply: str, n: int) -> Audit:
    seen: dict[int, tuple[bool, str]] = {}
    for line in (reply or "").splitlines():
        m = _VERDICT.match(line.strip())
        if not m or not 1 <= int(m.group(1)) <= n:
            continue
        # A directive read both ways counts as broken.
        i = int(m.group(1))
        broke = m.group(2).upper() == "VIOLATED"
        if broke or i not in seen:
            seen[i] = (broke, m.group(3).strip())
    return Audit(
        violated=[(i, seen[i][1]) for i in sorted(seen) if seen[i][0]],
        unchecked=[i for i in range(1, n + 1) if i not in seen])


def unchecked(n: int) -> Audit:
    return Audit(unchecked=list(range(1, n + 1)))


def revision_note(audit: Audit, docs) -> str:
    """The re-ask: each broken directive with its text, then the whole
    answer again, since the reply replaces the first one."""
    lines = ["The operator's directives bind the answer, and yours breaks "
             "these:"]
    for i, what in audit.violated:
        lines.append(f"- Directive: {docs[i - 1].text.strip()}\n"
                     f"  Broken by: {what}")
    lines.append("Rewrite the whole answer so it follows them, and change "
                 "nothing else. If one cannot be followed, say so openly and "
                 "give the reason.")
    return "\n".join(lines)


def enforce(text: str, audit: Audit, docs) -> str:
    """State what the delivered answer owes. The answer is never cut."""
    if audit.ok:
        return text
    lines = [HEADING, ""]
    for i, what in audit.violated:
        lines.append(f"- **Not followed:** {docs[i - 1].text.strip()}\n"
                     f"  ({what})")
    for i in audit.unchecked:
        lines.append(f"- **Not checked:** {docs[i - 1].text.strip()}")
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"


__all__ = ["AUDIT_SYSTEM", "Audit", "BINDING_NOTE", "HEADING", "NOT_DIRECTIVES",
           "audit_prompt", "directives_of", "enforce", "parse_audit",
           "revision_note", "unchecked"]
