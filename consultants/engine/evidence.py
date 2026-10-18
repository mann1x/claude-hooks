"""The evidence bar, held in code (council run 2, mails #1625 / #1627).

Prompts already say a defect is established only when a report shows
how it was checked. Run csl-2026-10-18-1120-95ce showed prompts are not
enough:

- the critic answered ``ready`` at 0.9 while writing "the researcher was
  unable to execute tests due to a read-only environment";
- the synthesizer ranked five defects, one refuted and three never
  triggered, with no ``Unverified`` section at all, and gave the
  council credit for two findings copied from the checkpoint brief.

So, on a run with the shell:

- :func:`hold_to_evidence_bar` turns a critic ``ready`` that says the
  tests were not run into ``needs_more_research``. The round caps still
  bound the loop.
- :func:`commands_block` gives the synthesizer every ``run_command``
  the lanes ran, with its exit code and the tail of its output, read
  from ``transcript.db``. Without it, the synthesizer sees only what the
  reports *say* they ran.
- :func:`needs_unverified_revision` / :data:`REVISION_NOTE`: an answer
  with no ``Unverified`` section is asked to revise once, and gets
  :data:`MISSING_UNVERIFIED_STUB` if it still has none.

Off (no shell, no checkpoint brief), none of this runs and every prompt
is byte-identical to before.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

SHELL_TOOL = "run_command"
#: Commands listed for the synthesizer, newest kept when over.
COMMANDS_MAX_ROWS = 60
COMMANDS_MAX_CHARS = 14_000
OUTPUT_TAIL_CHARS = 280

#: The critic saying its lanes did not, or could not, run the tests.
TESTS_NOT_RUN = re.compile(
    r"\b(?:unable|not able|could ?n[o']t|can ?n[o']t|cannot|did ?n[o']t|"
    r"failed|were not able|was not able)\s+(?:to\s+)?(?:\w+\s+){0,3}?"
    r"(?:run|execute|exercise)\w*\b[^.\n]{0,60}?\btests?\b"
    r"|\btests?\b[^.\n]{0,40}?\b(?:were|was|have|has|had|are|is)\s+"
    r"(?:not|never)\s+(?:been\s+)?(?:run|executed)\b"
    r"|\bno tests? (?:were|was|have been|has been) (?:run|executed)\b"
    r"|\bwithout (?:running|executing) (?:any |the )?tests?\b",
    re.I)

HELD_NOTE = (
    "[harness] Held at needs_more_research by the evidence bar: this "
    "critique answered ready while saying the tests were not run "
    "(“{quote}”). On a run with the shell the researchers can "
    "run them; name which tests would settle each ranked finding.")

UNVERIFIED_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*|\*\*)\s*unverified\b", re.I | re.M)

REVISION_NOTE = (
    "Your answer has no `Unverified` section, which is required on this "
    "run. Rewrite the answer in full, keeping everything else as it is:\n"
    "- A defect may stay ranked, and a case may keep a pass mark or "
    "✓, only when a command in COMMANDS THE LANES RAN shows it, with "
    "its output, or a report cites the line it read together with the "
    "comments around it.\n"
    "- Move everything else to a `## Unverified` section, one line each: "
    "`<claim>: <what would check it>`. The section is required even "
    "when it is empty; then write `(none)`.\n"
    "- Mark a claim taken from a checkpoint brief `(from the checkpoint "
    "brief, not re-verified)` unless a command or a cited read in the "
    "reports checks it.")

MISSING_UNVERIFIED_STUB = (
    "## Unverified\n\n"
    "_The synthesizer gave no Unverified section. Treat every claim "
    "above that is not shown by a command's output or a cited read as "
    "unverified._")

BRIEF_ATTRIBUTION = (
    "Mark every claim you take from it `(from the checkpoint brief, not "
    "re-verified)` unless a report shows a command output or a cited "
    "read that checks it; otherwise the answer credits the council with "
    "findings it did not make.")


def hold_to_evidence_bar(decision: str, text: str) -> tuple[str, str, bool]:
    """``(decision, text, held)``: ``ready`` with an admission that the
    tests were not run becomes ``needs_more_research`` with a note."""
    if decision != "ready" or not text:
        return decision, text, False
    m = TESTS_NOT_RUN.search(text)
    if not m:
        return decision, text, False
    quote = " ".join(m.group(0).split())[:160]
    return ("needs_more_research",
            text.rstrip() + "\n\n" + HELD_NOTE.format(quote=quote), True)


def needs_unverified_revision(text: str) -> bool:
    return bool(text and text.strip()) and not UNVERIFIED_HEADING.search(text)


def with_unverified_stub(text: str) -> str:
    if not needs_unverified_revision(text):
        return text
    return text.rstrip() + "\n\n" + MISSING_UNVERIFIED_STUB + "\n"


def _command_of(args: str) -> str:
    try:
        a = json.loads(args or "{}")
    except ValueError:
        return (args or "")[:200]
    if not isinstance(a, dict):
        return str(a)[:200]
    if a.get("command"):
        return str(a["command"])
    argv = a.get("argv")
    if isinstance(argv, list):
        return " ".join(str(x) for x in argv)
    return json.dumps(a)[:200]


def _exit_of(output: str) -> str:
    head = (output or "").split("\n", 1)[0]
    m = re.match(r"exit=(\S+)(?: wall=\S+)? killed_by=(\S+)", head)
    if m:
        k = m.group(2)
        return f"exit {m.group(1)}" + (
            "" if k.lower() in ("none", "") else f", killed by {k}")
    if (output or "").startswith("error"):
        return "harness error"
    return "?"


def _tail(output: str) -> str:
    body = (output or "").split("\n", 1)[1] if "\n" in (output or "") else ""
    body = re.sub(r"^--- (?:stdout|stderr) \(\d+ chars\) ---\n?", "", body,
                  flags=re.M).strip()
    body = body.split("\n--- your view vs the user's tree ---")[0]
    if not body:
        return "(no output)" if not (output or "").startswith("error") \
            else output[:OUTPUT_TAIL_CHARS]
    return ("…" + body[-OUTPUT_TAIL_CHARS:]) if len(body) > OUTPUT_TAIL_CHARS \
        else body


def commands_ran(db_path) -> Optional[list[dict]]:
    """Every ``run_command`` call of the session, oldest first, or None
    when the transcript cannot be read."""
    if not db_path or not Path(db_path).exists():
        return None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = c.execute(
                "SELECT role, lane_idx, args, output FROM events "
                "WHERE kind = 'tool_call' AND tool = ? ORDER BY event_id",
                (SHELL_TOOL,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    return [{"role": r or "?", "lane": lane, "command": _command_of(a),
             "exit": _exit_of(o), "tail": _tail(o)}
            for r, lane, a, o in rows]


#: A command that runs tests or a build: its output is the evidence.
TEST_LIKE = re.compile(
    r"\b(?:vitest|jest|pytest|mocha|ava|tap|bun\s+(?:x\s+)?(?:--bun\s+)?"
    r"(?:test|vitest|jest)|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|"
    r"yarn\s+test|cargo\s+(?:test|check|build)|go\s+(?:test|vet|build)|"
    r"make\s+(?:test|check)|ctest|tsc|unittest|node\s+--test|deno\s+test)\b",
    re.I)


def commands_block(db_path) -> str:
    """The synthesizer's COMMANDS THE LANES RAN block ("" when the
    transcript cannot be read). Test and build runs, and anything that
    failed, come with the tail of their output; other commands (reads,
    git) are one line each."""
    rows = commands_ran(db_path)
    if rows is None:
        return ""
    head = ("COMMANDS THE LANES RAN (from the engine's own record, not the "
            "reports; only these were executed):")
    if not rows:
        return (head + "\n(none: no lane ran a command, so nothing in the "
                "reports was checked by running it)")
    tests = [r for r in rows if TEST_LIKE.search(r["command"])
             or not r["exit"].startswith("exit 0")]
    other = [r for r in rows if r not in tests]

    def one(r, with_tail):
        cmd = " ".join(r["command"].split())
        cmd = cmd if len(cmd) <= 240 else cmd[:240] + "…"
        ln = f"- [{r['role']} lane {r['lane']}] `{cmd}` → {r['exit']}"
        if with_tail:
            ln += "\n      " + r["tail"].replace("\n", "\n      ")
        return ln

    out = [head, f"Test / build runs and failures ({len(tests)}):"]
    total = 0
    shown = 0
    for r in reversed(tests):          # the latest first: they settle it
        ln = one(r, True)
        if shown >= COMMANDS_MAX_ROWS or total + len(ln) > COMMANDS_MAX_CHARS:
            break
        out.append(ln)
        total += len(ln)
        shown += 1
    if shown < len(tests):
        out.append(f"- … {len(tests) - shown} more not shown")
    if not tests:
        out.append("- (none: no lane ran a test or a build)")
    out.append(f"Other commands ({len(other)}, reads and git; their output "
               f"is not shown here):")
    room = max(0, COMMANDS_MAX_CHARS + 4000 - total)
    for i, r in enumerate(other):
        ln = one(r, False)
        if len(ln) > room:
            out.append(f"- … {len(other) - i} more")
            break
        out.append(ln)
        room -= len(ln)
    return "\n".join(out)


__all__ = ["hold_to_evidence_bar", "commands_block", "commands_ran",
           "needs_unverified_revision", "with_unverified_stub",
           "REVISION_NOTE", "BRIEF_ATTRIBUTION", "TESTS_NOT_RUN"]
