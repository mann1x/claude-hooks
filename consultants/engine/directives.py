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

The auditor's verdict is one model's reading, so a clean audit is evidence,
not proof: four replays of one answer gave three violations and one clean
pass (csl-2026-10-31-1351-147b). What this module owes is that a verdict is
never lost and a missing one is never read as a pass.
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

#: The audit's own output cap, thinking included. Replays of an 8-directive
#: audit on glm-5.3-flash took 9,712-19,635 tokens with thinking on. With it
#: off the same model reasoned into the reply past 96,000.
AUDIT_OUTPUT_TOKENS = 32_000

AUDIT_SYSTEM = (
    "You check a final answer against the operator's directives. Judge only "
    "what a directive instructs or forbids, including what it forbids until "
    "a condition holds. Facts a directive states are background, not "
    "instructions. An answer that openly declines an instruction and gives "
    "its reason has met it. An answer that breaks one in passing, in a "
    "single clause, has not.\n"
    "Number your verdicts by the DIRECTIVE headings. Reply with exactly one "
    "line per directive and nothing else:\n"
    "N MET\n"
    "N VIOLATED: <the instruction broken> -- \"<the answer's words>\"")

_VERDICT = re.compile(r"^\W*(\d+)\W+(MET|VIOLATED)\b\W*(.*)$", re.I)
_ITEM = re.compile(r"^[ \t]*(\d+)[.)][ \t]+", re.M)


def directives_of(docs) -> list:
    """The docs that bind: injected by an operator, with text."""
    return [d for d in docs or ()
            if getattr(d, "source", "user") not in NOT_DIRECTIVES
            and (getattr(d, "text", "") or "").strip()]


@dataclass(frozen=True)
class Directive:
    """One instruction the audit gives its own verdict."""
    text: str


def items_of(docs) -> list:
    """Each directive of ``docs``, one per numbered item: one inject can
    hold eight, and numbering by doc read such a doc as a single directive
    (csl-2026-10-31-1351-147b). Numbers must run 1, 2, 3, so a nested list
    stays inside its item. Text before item 1 is an item too, since it may
    instruct; a doc without a list is one item."""
    items: list = []
    for doc in docs or ():
        text = doc.text.strip()
        marks = []
        for m in _ITEM.finditer(text):
            if int(m.group(1)) == len(marks) + 1:
                marks.append(m)
        if len(marks) < 2:
            items.append(Directive(text))
            continue
        head = text[:marks[0].start()].strip()
        if head:
            items.append(Directive(head))
        ends = [m.start() for m in marks[1:]] + [len(text)]
        items.extend(Directive(text[m.end():end].strip())
                     for m, end in zip(marks, ends))
    return items


def audit_prompt(docs, answer: str) -> str:
    listed = "\n\n".join(f"DIRECTIVE {i}:\n{d.text.strip()}"
                         for i, d in enumerate(docs, start=1))
    return f"{listed}\n\nFINAL ANSWER:\n{answer}"


@dataclass
class Audit:
    """``violated``: (directive number, what was broken). ``unchecked``:
    numbers the auditor gave no verdict on. ``stray``: what a VIOLATED
    verdict numbered past the directives says was broken. ``cut``: the
    auditor ran out of output, so nothing it wrote counts."""
    violated: list = field(default_factory=list)
    unchecked: list = field(default_factory=list)
    stray: list = field(default_factory=list)
    cut: bool = False

    @property
    def broken(self) -> bool:
        return bool(self.violated or self.stray)

    @property
    def ok(self) -> bool:
        return not self.broken and not self.unchecked


def _last_block(reply: str) -> list:
    """The reply's last run of verdict lines. Reasoning that reaches the
    reply can draft verdicts it later changes; the block it ends on is the
    decision."""
    blocks: list = [[]]
    for line in (reply or "").splitlines():
        line = line.strip()
        m = _VERDICT.match(line)
        if m:
            blocks[-1].append(m)
        elif line and blocks[-1]:
            blocks.append([])
    return next((b for b in reversed(blocks) if b), [])


def parse_audit(reply: str, n: int, *, cut: bool = False) -> Audit:
    if cut:
        return Audit(unchecked=list(range(1, n + 1)), cut=True)
    seen: dict[int, tuple[bool, str]] = {}
    stray: list = []
    for m in _last_block(reply):
        i, broke = int(m.group(1)), m.group(2).upper() == "VIOLATED"
        if not 1 <= i <= n:
            # Never drop one: it can be the only sign of a broken directive.
            if broke:
                stray.append(m.group(3).strip())
        # A directive read both ways counts as broken.
        elif broke or i not in seen:
            seen[i] = (broke, m.group(3).strip())
    return Audit(
        violated=[(i, seen[i][1]) for i in sorted(seen) if seen[i][0]],
        unchecked=[i for i in range(1, n + 1) if i not in seen],
        stray=stray)


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
    lines.extend(f"- Broken by: {what}" for what in audit.stray)
    lines.append("Rewrite the whole answer so it follows them, and change "
                 "nothing else. If one cannot be followed, say so openly and "
                 "give the reason.")
    return "\n".join(lines)


def enforce(text: str, audit: Audit, docs) -> str:
    """State what the delivered answer owes. The answer is never cut."""
    if audit.ok:
        return text
    lines = [HEADING, ""]
    if audit.cut:
        lines.append("- **Audit incomplete:** the auditor reached its output "
                     "limit before giving a verdict.")
    for i, what in audit.violated:
        lines.append(f"- **Not followed:** {docs[i - 1].text.strip()}\n"
                     f"  ({what})")
    lines.extend(f"- **Not followed:** {what}" for what in audit.stray)
    for i in audit.unchecked:
        lines.append(f"- **Not checked:** {docs[i - 1].text.strip()}")
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"


__all__ = ["AUDIT_OUTPUT_TOKENS", "AUDIT_SYSTEM", "Audit", "BINDING_NOTE",
           "Directive", "HEADING", "NOT_DIRECTIVES", "audit_prompt",
           "directives_of", "enforce", "items_of", "parse_audit",
           "revision_note", "unchecked"]
