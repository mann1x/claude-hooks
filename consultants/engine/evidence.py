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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SHELL_TOOL = "run_command"
#: Commands listed for the synthesizer, newest kept when over.
COMMANDS_MAX_ROWS = 60
COMMANDS_MAX_CHARS = 24_000
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


#: Lines a test printed, shown with the run (run 3: P3b-P3d were in the
#: log lines of an "11 passed" vitest run, which the tail cut off).
PRINTED_MAX_CHARS = 3000
PRINTED_LINE_CHARS = 200
#: Runner noise that is not something the test printed.
_RUNNER_NOISE = re.compile(
    r"^\s*(?:RUN\s+v[\d.]+|[✓✗×√]\s|Test Files\b|Tests\b|Start at\b|"
    r"Duration\b|Transform\b|Setup\b|Collect\b|Environment\b|Prepare\b|"
    r"=+ .* =+$|platform \w+ -- Python|rootdir:|collected \d+ items?|"
    r"plugins:|cachedir:)")
_VITEST_STDOUT = re.compile(r"^(?:stdout|stderr) \| (\S+)(?: > (.*))?$")


def _body(output: str) -> str:
    body = (output or "").split("\n", 1)[1] if "\n" in (output or "") else ""
    body = re.sub(r"^--- (?:stdout|stderr) \(\d+ chars\) ---\n?", "", body,
                  flags=re.M)
    return body.split("\n--- your view vs the user's tree ---")[0].strip()


def _tail(output: str) -> str:
    body = _body(output)
    printed, summary = _split_summary(body)
    if printed.strip() and summary.strip():
        body = summary.strip("\n")
    if not body:
        return "(no output)" if not (output or "").startswith("error") \
            else output[:OUTPUT_TAIL_CHARS]
    return ("…" + body[-OUTPUT_TAIL_CHARS:]) if len(body) > OUTPUT_TAIL_CHARS \
        else body


#: Where a runner's own summary starts (the per-file ✓ lines included).
_SUMMARY_START = re.compile(
    r"^\s*(?:[✓✗×❯√]\s|Test Files\b|Tests:?\s|=+ [^\n]*(?:passed|failed|"
    r"error)|[1-9]\d*\s+(?:pass|fail|passing|failing)\b|(?:PASS|FAIL)\s|"
    r"ok\s+\S+\s+[\d.]+s|test result:)")


def _split_summary(body: str) -> tuple[str, str]:
    """``(printed part, summary part)``: the summary begins at the first
    summary line among the last 30, so a printed line is never split."""
    lines = body.split("\n")
    for i in range(max(0, len(lines) - 30), len(lines)):
        if _SUMMARY_START.match(lines[i]):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    if len(body) <= OUTPUT_TAIL_CHARS:
        return "", body
    return body[:-OUTPUT_TAIL_CHARS], body[-OUTPUT_TAIL_CHARS:]


def _printed(output: str) -> str:
    """What a test run printed before its summary (vitest's ``stdout |
    file > suite > test`` blocks become ``· test: line``), cut at
    :data:`PRINTED_MAX_CHARS`; "" when it printed nothing of its own."""
    head, _ = _split_summary(_body(output))
    out: list[str] = []
    label = ""
    for ln in head.split("\n"):
        m = _VITEST_STDOUT.match(ln.strip())
        if m:
            label = (m.group(2) or m.group(1)).split(" > ")[-1]
            continue
        if not ln.strip() or _RUNNER_NOISE.match(ln):
            continue
        ln = ln.strip()
        if len(ln) > PRINTED_LINE_CHARS:
            ln = ln[:PRINTED_LINE_CHARS] + "…"
        out.append(f"· {label}: {ln}" if label else ln)
        label = ""
    text = "\n".join(out)
    if len(text) > PRINTED_MAX_CHARS:
        text = text[:PRINTED_MAX_CHARS] + "\n…(more printed; cut)"
    return text


def commands_ran(db_path) -> Optional[list[dict]]:
    """Every ``run_command`` call of the session, oldest first, or None
    when the transcript cannot be read. ``id`` is ``C<n>``, the name the
    synthesizer cites a run by; ``printed`` is what a test printed."""
    if not db_path or not Path(db_path).exists():
        return None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = c.execute(
                "SELECT event_id, role, lane_idx, args, output FROM events "
                "WHERE kind = 'tool_call' AND tool = ? ORDER BY event_id",
                (SHELL_TOOL,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    out = []
    for n, (eid, r, lane, a, o) in enumerate(rows, start=1):
        cmd = _command_of(a)
        out.append({"id": f"C{n}", "n": n, "event_id": eid,
                    "role": (r or "?").split(":")[0], "lane": lane,
                    "command": cmd, "exit": _exit_of(o), "tail": _tail(o),
                    "printed": _printed(o) if is_probe(cmd) else "",
                    "ran": _reached_the_code(cmd, o),
                    "_body": _body(o)[:4000]})
    return out


#: A command that runs tests or a build: its output is the evidence.
TEST_LIKE = re.compile(
    r"\b(?:vitest|jest|pytest|mocha|ava|tap|bun\s+(?:x\s+)?(?:--bun\s+)?"
    r"(?:test|vitest|jest)|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|"
    r"yarn\s+test|cargo\s+(?:test|check|build)|go\s+(?:test|vet|build)|"
    r"make\s+(?:test|check)|ctest|tsc|unittest|node\s+--test|deno\s+test)"
    r"(?![.\w/-])",          # not ``vitest.config.ts`` / ``jest/``
    re.I)
#: A run that reached its tests: a non-zero count of tests (vitest /
#: jest ``Tests  3 passed``, pytest ``== 3 passed in``), a TAP line, a
#: go verdict. A ``FAIL`` banner or ``Test Files 1 failed`` alone is not
#: one: a suite that does not load prints both.
TEST_RESULTS = re.compile(
    r"^\s*Tests:?\s+(?:[^\n]*?\b)?[1-9]\d*\s+(?:passed|failed)|"
    r"^=+ [^\n]*\b[1-9]\d* (?:passed|failed)|^\s*(?:not )?ok \d|"
    r"--- (?:PASS|FAIL):|^ok\s+\S+\s+[\d.]+s|"
    r"^\s*[1-9]\d*\s+(?:pass|fail|passing|failing)\b", re.M)  # bun, mocha
#: Looser, for a runner whose summary is not one of the above.
ANY_COUNT = re.compile(r"\b[1-9]\d*\s+(?:tests?\s+)?(?:passed|failed)\b")
#: A run that never got to the code it meant to exercise.
SETUP_FAILED = re.compile(
    r"failed to load config|Startup Error|Cannot find (?:module|package)|"
    r"UNRESOLVED_IMPORT|DNSResolveFailed|can't cd|No test files found|"
    r"command not found|\(0 tests?\)|Failed to resolve import", re.I)
#: A command that executes code written to probe a behaviour.
RUNS_CODE = re.compile(
    r"(?:^|[;&|(]\s*|\s)(?:node|bun|deno|tsx|ts-node|python3?|ruby|perl|"
    r"go\s+run|cargo\s+run|npx)\s+(?!--version\b|-v\b)\S", re.I)


def _body_of(r: dict) -> str:
    return r.get("_body") or r.get("tail") or ""


def _reached_the_code(cmd: str, output: str) -> bool:
    """Whether a probe run got to the code it meant to exercise: a test
    run that reports results, or a probe that ran without a setup
    failure. A config that does not load, a bad ``cd`` or a missing
    module is not evidence about the code."""
    if not is_probe(cmd) or _exit_of(output) in ("harness error", "?"):
        return False
    body = _body(output)
    if TEST_LIKE.search(cmd):
        return bool(TEST_RESULTS.search(body)) or (
            bool(ANY_COUNT.search(body)) and not SETUP_FAILED.search(body))
    return bool(body) and not SETUP_FAILED.search(body)


def is_probe(command: str) -> bool:
    """A test, a build, or code run to see what it does: a run whose
    output can show a defect. Reads, listings and git are not."""
    return bool(TEST_LIKE.search(command or "") or RUNS_CODE.search(command or ""))


def _norm(cmd: str) -> str:
    return " ".join((cmd or "").split())


def probe_groups(rows: list[dict]) -> list[list[dict]]:
    """Probe runs grouped by identical command, oldest group first; the
    last row of a group is its latest run."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if is_probe(r["command"]):
            groups.setdefault(_norm(r["command"]), []).append(r)
    return sorted(groups.values(), key=lambda g: g[0]["n"])


EVIDENCE_RULES = (
    "Each command has an id (C1, C2, …). In your answer:\n"
    "- Every ranked defect cites the id of the run that shows it, e.g. "
    "`(C41)`. A defect you only read in the code, and no run shows, "
    "goes under `## Read, not run` with the line you read and the run "
    "that would settle it. It is never ranked.\n"
    "- Every test or probe run listed below is cited where you use what "
    "it showed, or listed under `## Dropped` with the reason (a rerun, "
    "a harness error, a finding another run supersedes). A run backing a "
    "finding no other lane has is the last one to drop.\n"
    "- Read what a test printed, not only its pass count: a test that "
    "only logs passes whatever the code does, and its evidence is in "
    "the printed lines.")


def commands_block(db_path, *, rules: bool = True) -> str:
    """The COMMANDS THE LANES RAN block ("" when the transcript cannot be
    read). Each distinct test or probe run comes with what it printed and
    the tail of its output; failures with their tail; other commands
    (reads, git) are one line each. ``rules``: the synthesizer's
    citation rules (:data:`EVIDENCE_RULES`); the critic reads the block
    without them."""
    rows = commands_ran(db_path)
    if rows is None:
        return ""
    head = ("COMMANDS THE LANES RAN (from the engine's own record, not the "
            "reports; only these were executed):")
    if not rows:
        return (head + "\n(none: no lane ran a command, so nothing in the "
                "reports was checked by running it)")
    all_groups = probe_groups(rows)
    groups = [g for g in all_groups if g[-1]["ran"]]
    stuck = [g for g in all_groups if not g[-1]["ran"]]
    grouped = {r["n"] for g in all_groups for r in g}
    failed = [r for r in rows if r["n"] not in grouped
              and not r["exit"].startswith("exit 0")]
    other = [r for r in rows if r["n"] not in grouped and r not in failed]

    def one(r, *, with_tail=False, printed=False, same=(), first=False):
        cmd = _norm(r["command"])
        cmd = cmd if len(cmd) <= 240 else cmd[:240] + "…"
        ln = f"- {r['id']} [{r['role']} lane {r['lane']}] `{cmd}` → {r['exit']}"
        if same:
            ln += f" (same command also ran as {', '.join(same)})"
        if first:
            why = next((x.strip() for x in _body_of(r).split("\n")
                        if SETUP_FAILED.search(x)), "") or \
                r["tail"].strip().split("\n")[-1]
            ln += f": {why[:200]}"
        if printed and r.get("printed"):
            ln += "\n      printed:\n      " + r["printed"].replace(
                "\n", "\n      ")
        if with_tail:
            ln += "\n      " + ("end of output:\n      " if printed and
                                r.get("printed") else "") + \
                r["tail"].replace("\n", "\n      ")
        return ln

    out = [head] + ([EVIDENCE_RULES] if rules else []) + [
        f"Test and probe runs that reached the code ({len(groups)} "
        f"distinct, latest run of each):"]
    total = shown = 0
    for g in reversed(groups):          # the latest first: they settle it
        ln = one(g[-1], with_tail=True, printed=True,
                 same=[r["id"] for r in g[:-1]])
        if shown >= COMMANDS_MAX_ROWS or total + len(ln) > COMMANDS_MAX_CHARS:
            break
        out.append(ln)
        total += len(ln)
        shown += 1
    if shown < len(groups):
        rest = [g[-1]["id"] for g in groups[:len(groups) - shown]]
        out.append(f"- … {len(rest)} older runs not shown here: "
                   f"{', '.join(rest)}")
    if not groups:
        out.append("- (none: no test or probe run reached the code)")
    if stuck:
        out.append(f"Test and probe runs that never reached the code "
                   f"({len(stuck)}; a setup failure is not evidence about "
                   f"the code):")
        for g in stuck[-30:]:
            out.append(one(g[-1], same=[r["id"] for r in g[:-1]], first=True))
    if failed:
        out.append(f"Other commands that failed ({len(failed)}):")
        for r in failed[-20:]:
            out.append(one(r, with_tail=True))
    out.append(f"Other commands ({len(other)}, reads and git; their output "
               f"is not shown here):")
    room = max(0, COMMANDS_MAX_CHARS // 3)
    for i, r in enumerate(other):
        ln = one(r)
        if len(ln) > room:
            out.append(f"- … {len(other) - i} more")
            break
        out.append(ln)
        room -= len(ln)
    return "\n".join(out)


# ------------------------------------------------------------ the audit
#: ``C12``, ``C3-C9`` / ``C3–9``: run ids as the answer cites them.
CMD_REF = re.compile(r"\bC(\d{1,4})(?:\s*[–-]\s*C?(\d{1,4}))?\b")
_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+(.+?)\s*#*\s*$|\*\*([^*\n]+?)"
                      r"\*\*\s*:?\s*$)")
#: Sections whose items are ranked defects.
DEFECT_SECTION = re.compile(
    r"defect|bug|finding|issue|vulnerab|flaw|problem|regression", re.I)
#: Sections that hold what is not ranked.
UNRANKED_SECTION = re.compile(
    r"unverified|dropped|read,? not run|not run|fixed|usefulness|"
    r"improvement|recommend|reducer|invariant|sandbox", re.I)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}")
_ITEM = re.compile(r"^(\s{0,3})(?:[-*+]|\d{1,3}[.)])\s+\S")
READ_NOT_RUN_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*|\*\*)\s*read,? not run\b", re.I | re.M)
DROPPED_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s*|\*\*)\s*dropped\b",
                             re.I | re.M)
MAX_LISTED = 25


def cited(text: str) -> set[int]:
    out: set[int] = set()
    for m in CMD_REF.finditer(text or ""):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a <= b <= a + 200:
            out.update(range(a, b + 1))
        else:
            out.add(a)
    return out


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """``(heading, first_line, end_line)`` for each headed section."""
    marks = []
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln)
        if m:
            marks.append((i, (m.group(1) or m.group(2) or "").strip()))
    out = []
    for k, (i, h) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        out.append((h, i + 1, end))
    return out


def ranked_items(text: str) -> list[dict]:
    """The ranked defects of an answer: table rows and top-level list
    items under a defect heading. Each is ``{start, end, text}`` over
    the answer's lines (``end`` exclusive)."""
    lines = (text or "").split("\n")
    items: list[dict] = []
    for heading, a, b in _sections(lines):
        if not DEFECT_SECTION.search(heading) or UNRANKED_SECTION.search(heading):
            continue
        tops = [len(m.group(1)) for ln in lines[a:b]
                for m in [_ITEM.match(ln)] if m]
        top = min(tops) if tops else None
        cur: Optional[dict] = None
        for i in range(a, b):
            ln = lines[i]
            s = ln.strip()
            if s.startswith("|"):
                cur = None
                if _TABLE_SEP.match(s):
                    continue
                if i + 1 < b and _TABLE_SEP.match(lines[i + 1].strip()):
                    continue          # the header row
                items.append({"start": i, "end": i + 1, "text": ln})
                continue
            m = _ITEM.match(ln)
            if m and len(m.group(1)) == top:
                cur = {"start": i, "end": i + 1, "text": ln}
                items.append(cur)
            elif cur is not None and (s == "" or ln[:1].isspace()):
                cur["end"] = i + 1
                cur["text"] += "\n" + ln
            else:
                cur = None
    for it in items:
        while it["end"] - 1 > it["start"] and not lines[it["end"] - 1].strip():
            it["end"] -= 1
        it["text"] = "\n".join(lines[it["start"]:it["end"]])
    return items


def _label(item_text: str) -> str:
    s = item_text.strip()
    if s.startswith("|"):
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and re.fullmatch(r"\**\s*#?\d+\s*\**", cells[0] or ""):
            cells = cells[1:]
        s = " — ".join(c for c in cells[:2] if c)
    else:
        s = re.sub(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+", "", s.split("\n")[0])
    s = re.sub(r"\*\*|__", "", s).strip().rstrip(".;:,")
    return s if len(s) <= 220 else s[:220] + "…"


@dataclass
class Audit:
    missing_unverified: bool = False
    #: Ranked items that cite no probe run.
    unbacked: list = field(default_factory=list)
    #: Probe groups the answer does not mention.
    uncited: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_unverified or self.unbacked or self.uncited)


def audit_answer(text: str, rows: Optional[list[dict]]) -> Audit:
    """What the answer owes the evidence bar. ``rows`` None (no shell):
    only the Unverified section is checked."""
    a = Audit(missing_unverified=needs_unverified_revision(text))
    if rows is None:
        return a
    groups = [g for g in probe_groups(rows) if g[-1]["ran"]]
    probe_ns = {r["n"] for g in groups for r in g}
    a.unbacked = [it for it in ranked_items(text)
                  if not (cited(it["text"]) & probe_ns)]
    refs = cited(text)
    a.uncited = [g for g in groups if not any(r["n"] in refs for r in g)]
    return a


def _run_line(g: list[dict]) -> str:
    r = g[-1]
    cmd = _norm(r["command"])
    cmd = cmd if len(cmd) <= 160 else cmd[:160] + "…"
    first = next((ln for ln in (r.get("printed") or "").split("\n")
                  if ln.strip()), "")
    verdict = next((m.group(0).strip() for m in [re.search(
        r"^\s*Tests:?\s+[^\n]*$|^=+ [^\n]*(?:passed|failed)[^\n]*$",
        _body_of(r), re.M)] if m), "")
    ids = ", ".join(x["id"] for x in g)
    return (f"{ids} [{r['role']} lane {r['lane']}] `{cmd}` → {r['exit']}"
            + (f" ({' '.join(verdict.split())})" if verdict else "")
            + (f"; it printed: {first[:200]}" if first else ""))


def revision_note(a: Audit) -> str:
    parts = []
    if a.missing_unverified:
        parts.append(REVISION_NOTE)
    else:
        parts.append("Your answer does not meet the evidence bar of this run. "
                     "Rewrite it in full, keeping everything else as it is:")
    if a.unbacked:
        parts.append(
            "- These ranked defects cite no run that shows them (a test or "
            "probe id from COMMANDS THE LANES RAN). Cite the run, or move "
            "each to `## Read, not run` (with the line read and the run "
            "that would settle it) or `## Unverified`:\n" + "\n".join(
                f"  - {_label(it['text'])}" for it in a.unbacked[:MAX_LISTED]))
    if a.uncited:
        parts.append(
            "- These runs are not mentioned anywhere in your answer. For "
            "each, carry what it showed (citing its id), or list it under "
            "`## Dropped` with the reason:\n" + "\n".join(
                f"  - {_run_line(g)}" for g in a.uncited[:MAX_LISTED])
            + (f"\n  - … and {len(a.uncited) - MAX_LISTED} more"
               if len(a.uncited) > MAX_LISTED else ""))
    return "\n".join(parts)


MOVED_NOTE = ("moved here by the harness: it was ranked, but it cites no "
              "run that shows it")
DROPPED_BY_OMISSION = (
    "## Dropped by omission\n\n"
    "_The answer does not mention these runs, so what they showed was not "
    "weighed. Read their output before relying on the answer:_\n")


def _insert_under(lines: list[str], heading_rx, new: list[str]) -> bool:
    for h, a, b in _sections(lines):
        if heading_rx.match("## " + h) or heading_rx.match(f"**{h}**"):
            at = b
            while at > a and not lines[at - 1].strip():
                at -= 1
            body = [ln for ln in lines[a:at] if ln.strip()]
            if len(body) == 1 and body[0].strip().strip("_*()").lower() == "none":
                del lines[a:at]
                at = a
            lines[at:at] = new
            return True
    return False


def enforce(text: str, a: Audit) -> str:
    """The answer with what still fails the bar after the revision made
    visible: unbacked ranked items moved to ``Unverified``, unmentioned
    runs listed under ``Dropped by omission``, the stub when there is no
    Unverified section."""
    if a.ok:
        return text
    lines = text.split("\n")
    moved = []
    for it in sorted(a.unbacked, key=lambda x: -x["start"]):
        moved.insert(0, f"- {_label(it['text'])}: {MOVED_NOTE}.")
        del lines[it["start"]:it["end"]]
    if moved:
        if not _insert_under(lines, UNVERIFIED_HEADING, moved):
            lines += ["", "## Unverified", ""] + moved
    out = "\n".join(lines).rstrip()
    out = with_unverified_stub(out).rstrip()
    if a.uncited:
        out += "\n\n" + DROPPED_BY_OMISSION + "\n".join(
            f"- {_run_line(g)}" for g in a.uncited[:MAX_LISTED])
        if len(a.uncited) > MAX_LISTED:
            out += f"\n- … and {len(a.uncited) - MAX_LISTED} more"
    return out + "\n"


# ------------------------------------------------- the critic and the ledger
CRITIC_LEDGER_NOTE = (
    "Check every claim a report marks verified, confirmed or reproduced "
    "against that lane's own runs below. A claim its own output "
    "contradicts is refuted: say so, and name the run. A test that only "
    "prints passes whatever the code does; read what it printed.")


# ---------------------------------------------- held tests ("Sandbox changes")
TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+[._-](?:test|spec)s?\.[^/]+"
                       r"|[^/]+_test\.[^/]+)$")
_WRITES = r"(?:>>?|\btee(?:\s+-a)?)\s*['\"]?\S*{name}"


def held_test_notes(db_path, lane: str, paths: list[str]) -> dict[str, str]:
    """For each test file a lane holds, a warning when it was not run
    after its last write, or its last run there failed. ``lane`` is the
    view's name (``researcher-100``, ``coder-0``). ``{path: note}``,
    only for files with something to say."""
    role, _, idx = lane.rpartition("-")
    if not role or not idx.isdigit() or not db_path or not Path(db_path).exists():
        return {}
    tests = [p for p in paths if TEST_FILE.search(p)]
    if not tests:
        return {}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = c.execute(
                "SELECT event_id, tool, args, output FROM events WHERE "
                "kind = 'tool_call' AND lane_idx = ? AND (role = ? OR role "
                "LIKE ?) AND tool IN ('write_file', ?) ORDER BY event_id",
                (int(idx), role, role + ":%", SHELL_TOOL)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return {}
    notes = {}
    for p in tests:
        name = re.escape(Path(p).name)
        last_write = None
        runs = []
        for eid, tool, args, output in rows:
            if tool == "write_file":
                try:
                    wp = str((json.loads(args or "{}") or {}).get("path") or "")
                except ValueError:
                    wp = ""
                if wp and (wp == p or wp.endswith("/" + p) or p.endswith(wp)):
                    last_write, runs = eid, []
                continue
            cmd = _command_of(args)
            if not re.search(name, cmd):
                continue
            if re.search(_WRITES.format(name=name), cmd) or re.search(
                    r"<<", cmd):
                last_write, runs = eid, []
            if TEST_LIKE.search(cmd):
                runs.append(_exit_of(output))
        if last_write is None:
            continue
        if not runs:
            notes[p] = "⚠ never run after it was written"
        elif not runs[-1].startswith("exit 0"):
            notes[p] = f"⚠ its last run failed ({runs[-1]})"
    return notes


__all__ = ["hold_to_evidence_bar", "commands_block", "commands_ran",
           "needs_unverified_revision", "with_unverified_stub",
           "audit_answer", "revision_note", "enforce", "Audit",
           "ranked_items", "probe_groups", "is_probe", "cited",
           "held_test_notes", "REVISION_NOTE", "BRIEF_ATTRIBUTION",
           "CRITIC_LEDGER_NOTE", "EVIDENCE_RULES", "TESTS_NOT_RUN"]
