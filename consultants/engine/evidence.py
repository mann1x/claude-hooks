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
from typing import Iterable, Optional

from claude_hooks import test_reruns as _tr

SHELL_TOOL = "run_command"
#: Commands listed for a reader whose window is not known, newest kept
#: when over. A reader with a known window gets
#: ``commands_budget_chars()`` instead (run 7: 24,000 characters hid 53
#: runs, the coder's decisive C152/C153/C161 among them, from a
#: synthesizer whose window held all 104,000).
COMMANDS_MAX_ROWS = 10_000
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

#: A heading, ``### ## Unverified`` included (run 7 wrote that, and the
#: harness took the section for missing and appended its stub).
_HEAD = r"^\s{0,3}(?:(?:#{1,6}\s*)+|\*\*)\s*"
UNVERIFIED_HEADING = re.compile(_HEAD + r"unverified\b", re.I | re.M)

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


NO_RUN_NOTE = (
    "[harness] Held at needs_more_research by the evidence bar: this "
    "critique answered ready, but no command the lanes ran on this run "
    "reached the code (COMMANDS THE LANES RAN shows {what}). A ready "
    "verdict on a run with the shell needs at least one test or probe "
    "that ran. Name the command that would settle each ranked finding.")


NO_RUN_LANES_NOTE = (
    "[harness] Held at needs_more_research by the evidence bar: this "
    "critique answered ready, but {n} of the {m} lanes of this round ran "
    "nothing that reached the code (lanes {lanes}), so most of what they "
    "report was read, not run. Name the test or probe that would settle "
    "each of their findings.")
LANES_UNRUN_NOTE = (
    "[harness] Lanes {lanes} of this round ran nothing that reached the "
    "code: their findings rest on reading alone. Treat each as "
    "unverified unless another lane's run shows it. Confidence capped at "
    "{cap} by the evidence bar{said}.")
#: Run 8: the critique's CONFIDENCE line is stripped downstream, so the
#: note says what the critic claimed (ollama read the raw 0.95).
LANES_UNRUN_SAID = " (the critic rated it {said})"
#: The most a ready critique may claim while some lanes ran nothing.
UNRUN_CONFIDENCE_CAP = 0.6
_CONF_LINE = re.compile(
    r"(?im)^([ \t>*-]*CONFIDENCE[ \t]*[:=][ \t]*)([01](?:\.\d+)?|0?\.\d+)\b")


def round_lanes(db_path) -> Optional[list[int]]:
    """The researcher lanes of the round the critic is judging: those
    whose ``node_exit`` comes after the last critic or meta-critic exit.
    None when the transcript cannot be read."""
    if not db_path or not Path(db_path).exists():
        return None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            last = c.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM events WHERE kind = "
                "'node_exit' AND role IN ('critic', 'meta_critic')").fetchone()[0]
            lanes = [r[0] for r in c.execute(
                "SELECT DISTINCT lane_idx FROM events WHERE kind = 'node_exit' "
                "AND role = 'researcher' AND event_id > ? AND lane_idx IS NOT "
                "NULL ORDER BY lane_idx", (last,))]
        finally:
            c.close()
    except sqlite3.Error:
        return None
    return lanes


def _cap_confidence(text: str, cap: float) -> str:
    def sub(m):
        try:
            v = float(m.group(2))
        except ValueError:
            return m.group(0)
        return m.group(0) if v <= cap else f"{m.group(1)}{cap}"
    return _CONF_LINE.sub(sub, text)


def hold_to_evidence_bar(decision: str, text: str,
                         rows: Optional[list[dict]] = None,
                         lanes: Optional[list[int]] = None,
                         ) -> tuple[str, str, bool]:
    """``(decision, text, held)``: ``ready`` becomes
    ``needs_more_research`` with a note when the critique admits the
    tests were not run, or when the record (``rows``, from
    :func:`commands_ran`) shows no run that reached the code (council
    run 4, mail #1636: a critic read ready off reports, none of whose
    runs had loaded). ``rows=None`` means the record could not be read
    and decides nothing."""
    if decision != "ready" or not text:
        return decision, text, False
    m = TESTS_NOT_RUN.search(text)
    if m:
        quote = " ".join(m.group(0).split())[:160]
        return ("needs_more_research",
                text.rstrip() + "\n\n" + HELD_NOTE.format(quote=quote), True)
    if rows is not None and not any(r.get("ran") for r in rows):
        probes = sum(1 for r in rows if is_probe(r.get("command") or ""))
        what = ("no commands" if not rows else
                f"{len(rows)} command(s), {probes} of them tests or probes, "
                "none of which reached the code")
        return ("needs_more_research",
                text.rstrip() + "\n\n" + NO_RUN_NOTE.format(what=what), True)
    # Run 7 (mail #1659): ready at 1.0 while five of the round's eight
    # lanes ran nothing that reached the code.
    if rows is not None and lanes:
        ran = {r.get("lane") for r in rows
               if r.get("ran") and r.get("role") == "researcher"}
        unrun = [ln for ln in lanes if ln not in ran]
        names = ", ".join(str(x) for x in unrun)
        if len(unrun) * 2 > len(lanes):
            return ("needs_more_research", text.rstrip() + "\n\n"
                    + NO_RUN_LANES_NOTE.format(n=len(unrun), m=len(lanes),
                                               lanes=names), True)
        if unrun:
            capped = _cap_confidence(text, UNRUN_CONFIDENCE_CAP)
            said = [m.group(2) for m in _CONF_LINE.finditer(text)]
            higher = said and float(said[-1]) > UNRUN_CONFIDENCE_CAP
            return decision, capped.rstrip() + "\n\n" + LANES_UNRUN_NOTE.format(
                lanes=names, cap=UNRUN_CONFIDENCE_CAP,
                said=LANES_UNRUN_SAID.format(said=said[-1]) if higher else ""
            ), False
    return decision, text, False


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
                "SELECT event_id, role, lane_idx, args, output, ts FROM "
                "events WHERE kind = 'tool_call' AND tool = ? ORDER BY "
                "event_id",
                (SHELL_TOOL,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    out = []
    for n, (eid, r, lane, a, o, ts) in enumerate(rows, start=1):
        cmd = _command_of(a)
        out.append({"id": f"C{n}", "n": n, "event_id": eid, "ts": ts,
                    "role": (r or "?").split(":")[0], "lane": lane,
                    "command": cmd, "exit": _exit_of(o), "tail": _tail(o),
                    "printed": _printed(o) if is_probe(cmd) else "",
                    "ran": _reached_the_code(cmd, o),
                    "_body": _body(o)[:4000],
                    # The whole output, as the lane saw it (the shell
                    # bounds it at OUTPUT_MAX_CHARS): the auditor reads it.
                    "output": _body(o)})
    return out


#: A command that runs tests or a build (one definition, shared with the
#: agent loop's unrun-tests nudge).
TEST_LIKE = _tr.TEST_LIKE
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


NO_RUN_REPORT_NOTE = (
    "Before you report: none of the commands you ran on this run reached "
    "the code ({what}). On a run with the shell a report whose runs never "
    "reached the code is not evidence, however carefully it reads the "
    "source. Run the test or probe that would show your top finding, and "
    "fix its setup (a config, an import path, a `cd`) if it fails to "
    "load. If it cannot run here, say so in your report and why.")


def lane_runs(messages: list) -> list[tuple[str, str]]:
    """``(command, output)`` of every ``run_command`` call in a lane's
    conversation, in order."""
    calls: dict = {}
    order: list = []
    out: dict = {}
    for m in messages or []:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") != SHELL_TOOL:
                    continue
                a = fn.get("arguments")
                cid = tc.get("id") or f"_{len(order)}"
                calls[cid] = _command_of(a if isinstance(a, str) else json.dumps(a or {}))
                order.append(cid)
        elif m.get("role") == "tool" and m.get("tool_call_id") in calls:
            c = m.get("content")
            out[m["tool_call_id"]] = c if isinstance(c, str) else json.dumps(c)
    return [(calls[c], out.get(c, "")) for c in order]


def no_run_report_check(messages: list) -> Optional[str]:
    """A researcher's ``LoopConfig.report_check`` on a shell run: a note
    when none of its runs reached the code (council run 4, mail #1636),
    else None. A lane that ran nothing gets it as well."""
    runs = lane_runs(messages)
    if any(_reached_the_code(c, o) for c, o in runs):
        return None
    probes = sum(1 for c, _ in runs if is_probe(c))
    what = ("you ran none" if not runs else
            f"{len(runs)} command(s), {probes} of them tests or probes, none "
            "of which reached the code")
    return NO_RUN_REPORT_NOTE.format(what=what)


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
    "the printed lines.\n"
    "- A table row that marks a case ✓, ⚠ or ✗ (an edge-case matrix) "
    "cites the run that shows it, like a ranked defect. A row that cites "
    "none, or only a line read, has its mark replaced with `not tested`. "
    "Give the matrix a legend line under it (`✓ = handled correctly; ⚠ = "
    "…; ✗ = defect shown`): each mark is checked against what it says.")


def commands_block(db_path, *, rules: bool = True,
                   max_chars: Optional[int] = None) -> str:
    """The COMMANDS THE LANES RAN block ("" when the transcript cannot be
    read). Each distinct test or probe run comes with what it printed and
    the tail of its output; failures with their tail; other commands
    (reads, git) are one line each. ``rules``: the synthesizer's
    citation rules (:data:`EVIDENCE_RULES`); the critic reads the block
    without them."""
    rows = commands_ran(db_path)
    if rows is None:
        return ""
    budget = int(max_chars or COMMANDS_MAX_CHARS)
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
        if shown >= COMMANDS_MAX_ROWS or total + len(ln) > budget:
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
    room = max(0, budget // 3)
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
    r"improvement|recommend|reducer|invariant|sandbox|held", re.I)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}")
_ITEM = re.compile(r"^(\s{0,3})(?:[-*+]|\d{1,3}[.)])\s+\S")
READ_NOT_RUN_HEADING = re.compile(_HEAD + r"read,? not run\b", re.I | re.M)
DROPPED_HEADING = re.compile(_HEAD + r"dropped\b", re.I | re.M)
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
            h = (m.group(1) or m.group(2) or "").strip()
            marks.append((i, re.sub(r"^(?:#+\s*)+", "", h).strip()))
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


#: A reason for dropping a run that says what happened to it.
DROPPED_REASON_OK = re.compile(
    r"\bre-?run|\bduplicate|\bsame (?:command|run|test)|\bharness error|"
    r"\bsetup\b|did not load|never reached|failed to load|cannot find|"
    r"not found|\bno tests?\b|timed? ?out|\bkilled\b", re.I)
#: Thin reasons (run 6, mail #1647: "General test run", "Not needed for
#: final result" for ~25 runs) asked to be revised from this many on.
THIN_DROPPED_MIN = 3
#: One reason given verbatim to this many runs says nothing about each
#: (run 6: "Superseded by C387" for 12 runs, most not about C387's file).
REPEATED_REASON_MIN = 5
#: "superseded by X": what X is decides whether it is a reason.
SUPERSEDED = re.compile(r"\bsuperseded\s+by\s+[(`*\s]*([^\s)`*,;.]+)", re.I)
THIN_DROPPED_NOTE = ("harness: this reason does not say what the run "
                     "showed or why that does not change the answer")
_DROPPED_LINE = re.compile(
    r"^\s*[-*+]\s+\**\s*(C\d{1,4}(?:\s*[,–-]\s*C?\d{1,4})*)\s*\**"
    r"\s*(?:[:—–]|-\s)\s*(.+?)\s*$")


def thin_dropped(text: str) -> list[dict]:
    """The ``## Dropped`` entries whose reason is thin: ``{line, ids,
    reason}``. A reason is thin when it names no other run, is not a
    stated rerun / setup / harness reason, and has fewer than 8 words,
    or when it is given verbatim to :data:`REPEATED_REASON_MIN` runs or
    more, or when the run it is superseded by is itself dropped."""
    lines = (text or "").split("\n")
    entries = []
    for heading, a, b in _sections(lines):
        if not re.match(r"dropped\b", heading, re.I) or "omission" in heading.lower():
            continue
        for i in range(a, b):
            m = _DROPPED_LINE.match(lines[i])
            if m:
                entries.append({"line": i, "ids": m.group(1),
                                "reason": m.group(2).strip()})
    counts: dict[str, int] = {}
    for e in entries:
        k = " ".join(e["reason"].lower().strip(" .").split())
        counts[k] = counts.get(k, 0) + 1
    # Run 7: "superseded by (C211)" for eight runs, "superseded by
    # `audit-repro.test.ts`" — a file, not a run. A run is superseded by
    # another run, named; one run said to supersede many says nothing
    # about each.
    by_target: dict[int, int] = {}
    for e in entries:
        m = SUPERSEDED.search(e["reason"])
        if m and CMD_REF.match(m.group(1)):
            t = int(CMD_REF.match(m.group(1)).group(1))
            by_target[t] = by_target.get(t, 0) + max(1, len(cited(e["ids"])))
    # Run 8: "C49: Superseded by C49", "C64, C65: Superseded by C64" — a
    # run dropped in favour of itself, or of another run dropped too.
    dropped = set()
    for e in entries:
        dropped |= cited(e["ids"])
    out = []
    for e in entries:
        r = e["reason"]
        k = " ".join(r.lower().strip(" .").split())
        if THIN_DROPPED_NOTE in r:
            continue
        stock = not CMD_REF.search(r) and not DROPPED_REASON_OK.search(r) \
            and len(re.findall(r"\w+", r)) < 8
        sup = SUPERSEDED.search(r)
        not_a_run = bool(sup) and not CMD_REF.match(sup.group(1))
        target = (int(CMD_REF.match(sup.group(1)).group(1))
                  if sup and not not_a_run else None)
        many = target is not None and by_target.get(target, 0) >= REPEATED_REASON_MIN
        circular = target is not None and target in dropped
        if stock or not_a_run or many or circular \
                or counts[k] >= REPEATED_REASON_MIN:
            out.append(e)
    return out


@dataclass
class Audit:
    missing_unverified: bool = False
    #: Ranked items that cite no probe run.
    unbacked: list = field(default_factory=list)
    #: Probe groups the answer does not mention.
    uncited: list = field(default_factory=list)
    #: ``## Dropped`` entries with a thin reason (:func:`thin_dropped`).
    thin: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_unverified or self.unbacked or self.uncited
                    or len(self.thin) >= THIN_DROPPED_MIN)


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
    a.thin = thin_dropped(text)
    return a


def _quotable(printed: str) -> str:
    """The first line of what a run printed that can sit inside one line
    of the answer: a bare fence line (```` ```diff ````) is skipped and a
    fence inside a line is defused. Run 10b (csl-2026-10-26-1931-f8b3)
    quoted ```` ```diff ```` here, the answer then held an odd number of
    fences, and the truncation backstop failed a complete answer."""
    for ln in printed.split("\n"):
        s = ln.strip()
        if s and not s.startswith("```"):
            return ln.replace("```", "'''")
    return ""


def _run_line(g: list[dict]) -> str:
    r = g[-1]
    cmd = _norm(r["command"])
    cmd = cmd if len(cmd) <= 160 else cmd[:160] + "…"
    first = _quotable(r.get("printed") or "")
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
    if len(a.thin) >= THIN_DROPPED_MIN:
        parts.append(
            "- These `## Dropped` reasons do not say what the run showed. "
            "For each, say what it printed and why that does not change the "
            "answer, or name the run that supersedes it and what that run "
            "shows of the same code; one reason copied to many runs says "
            "nothing about each:\n" + "\n".join(
                f"  - {e['ids']}: {e['reason'][:120]}"
                for e in a.thin[:MAX_LISTED])
            + (f"\n  - … and {len(a.thin) - MAX_LISTED} more"
               if len(a.thin) > MAX_LISTED else ""))
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
    if len(a.thin) >= THIN_DROPPED_MIN:      # in place, before lines move
        for e in a.thin:
            lines[e["line"]] = lines[e["line"]].rstrip() + \
                f" _({THIN_DROPPED_NOTE})_"
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


# ------------------------------------------- does the cited run show it
# Council run 4 (mail #1636): ranked #2 cited a run that reached the code,
# so the audit above passed it, but the run's output did not show the
# defect. Citing a run is not the run showing it. An auditor that sees
# only the claim and what the cited runs printed, side by side, decides;
# the code moves what it finds not shown.

CLAIM_AUDIT_SYSTEM = (
    "You check claims against command output, and nothing else. RUN "
    "OUTPUTS gives each run's whole output once, under its id; each claim "
    "after it names the runs it cites. Decide, for each claim, whether "
    "the output of the runs it cites shows it:\n"
    "- SHOWN: a printed line states the defect or demonstrates it (a "
    "failing assertion, a wrong value printed, an error the claim "
    "predicts).\n"
    "- NOT SHOWN: the output is about something else, only shows that "
    "a test ran or passed, or shows a setup failure. A passing test does "
    "not show a defect unless what it printed does.\n"
    "- CONTRADICTED: the output shows the opposite of the claim.\n"
    "- PARTLY: the claim makes several assertions (clauses joined by "
    "\"and\", \"also\", a semicolon, a second consequence) and the output "
    "shows some of them but not all. Name the clauses it does not show.\n"
    "A matrix claim (\"<tool> on <case>: …\") is about how that tool "
    "behaves on that case, never about a table in the output: decide "
    "whether the output shows that behaviour.\n"
    "Judge every clause of a claim, not only its first: a claim is SHOWN "
    "only when the output shows all of it. "
    "Use only the output given; never what the claim says it shows. "
    "Answer one line per claim and nothing else:\n"
    "`A<k>: SHOWN|NOT SHOWN|CONTRADICTED|PARTLY — <the printed line you "
    "relied on, or what is missing>`")
CLAIM_AUDIT_MAX_ITEMS = 12
CLAIM_AUDIT_RUNS_PER_ITEM = 6
_VERDICT_LINE = re.compile(
    r"^\W*A(\d{1,3})\W*[:.)\]-]\s*\**\s*(NOT\s+SHOWN|CONTRADICTED|PARTLY|"
    r"SHOWN)\b\**\s*(?:[—–:-]+\s*)?(.*)$", re.I | re.M)
NOT_SHOWN_NOTE = ("moved here by the harness's audit: {verdict} by its cited "
                  "run(s) {ids} ({why})")
#: Council run 6 (mail #1647): ranked #2 said "launders and clobbers
#: concurrent writers"; its run showed only the first.
PARTLY_NOTE = ("only part of this is shown by {ids}; not shown: {why} "
               "(harness's audit)")
PARTLY_UNVERIFIED = "{label} — the part its runs {ids} do not show: {why}."


def _shown_output(r: dict) -> str:
    """A run's whole output for the auditor; what it printed and its
    tail when the full output is not on the row."""
    full = (r.get("output") or "").strip()
    if full:
        return full
    out = (r.get("printed") or "").strip()
    tail = (r.get("tail") or "").strip()
    if tail and tail not in out:
        out = (out + "\n" + tail).strip()
    return out or "(printed nothing)"


#: A case mark in a table cell (run 5's edge-case matrix).
CASE_MARK = re.compile(r"[✓✔⚠✗✘❌](?:️)?")
_CODE_CITE = re.compile(r"[\w./-]+\.\w{1,6}:\d+")
#: Marked cells audited, over all matrix rows.
MATRIX_MAX_ITEMS = 40


def _cells(row: str) -> list[str]:
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def _header(lines: list[str], i: int) -> list[str]:
    """The header cells of the table row ``lines[i]`` is in."""
    j = i
    while j > 0 and lines[j - 1].strip().startswith("|"):
        j -= 1
    if j + 1 < len(lines) and _TABLE_SEP.match(lines[j + 1].strip()):
        return [c.strip() for c in _cells(lines[j])]
    return []


#: What a mark asserts when the answer gives no legend (run 7: cells
#: went to the auditor as "the column `read_files` says '✓'", and every
#: one came back "no such table printed").
MARK_MEANING = {"✓": "handles this case correctly (no defect)",
                "✔": "handles this case correctly (no defect)",
                "⚠": "partly mishandles this case, or it is a risk",
                "✗": "mishandles this case: a defect",
                "✘": "mishandles this case: a defect",
                "❌": "mishandles this case: a defect"}
_LEGEND = re.compile(r"([✓✔⚠✗✘❌])\ufe0f?\s*(?:=|:|—|–|-|means)\s*([^;,|✓✔⚠✗✘❌\n]+)")


def _legend(lines: list[str], a: int, b: int) -> dict[str, str]:
    """The marks a table's legend defines, from the lines just above
    and below it (within its section)."""
    out: dict[str, str] = {}
    for ln in lines[max(a, 0):b]:
        if ln.strip().startswith("|"):
            continue
        for m in _LEGEND.finditer(ln):
            out.setdefault(m.group(1), m.group(2).strip(" .*_"))
    return out


def _cell_note(cells: list[str]) -> str:
    """A row's own words about its case: the cells that hold no mark,
    after the case name."""
    rest = [c.strip() for c in cells[1:] if c.strip() and not CASE_MARK.search(c)]
    return " ".join(rest)


def matrix_rows(text: str) -> list[dict]:
    """Table rows outside the ranked and unranked sections that mark a
    case ✓ / ⚠ / ✗ (council run 5, mail #1641: "Multibyte ✓" was wrong
    and cited a line read, not a run). ``{start, end, text, kind,
    cols}``: ``cols`` is ``[(column, header, cell)]`` of every marked
    cell (run 6, mail #1647: a row has a mark per tool)."""
    lines = (text or "").split("\n")
    ranked = {it["start"] for it in ranked_items(text)}
    out = []
    for heading, a, b in _sections(lines):
        if DEFECT_SECTION.search(heading) or UNRANKED_SECTION.search(heading):
            continue
        for i in range(a, b):
            s = lines[i].strip()
            if not s.startswith("|") or i in ranked or _TABLE_SEP.match(s):
                continue
            if i + 1 < b and _TABLE_SEP.match(lines[i + 1].strip()):
                continue                       # the header row
            if not CASE_MARK.search(lines[i]):
                continue
            head = _header(lines, i)
            cells = _cells(lines[i])
            cols = [(k, head[k] if k < len(head) else f"column {k + 1}",
                     c.strip()) for k, c in enumerate(cells)
                    if CASE_MARK.search(c)]
            out.append({"start": i, "end": i + 1, "text": lines[i],
                        "kind": "cell", "cols": cols,
                        "legend": _legend(lines, a, b),
                        "note": _cell_note(cells)})
    return out


def not_tested(row: str, why: str, cols: Optional[list[int]] = None) -> str:
    """``row`` with the case marks of ``cols`` (every column when None)
    replaced: the first by ``not tested (why)``, the rest by ``not
    tested``. Run 6 (mail #1647): replacing only the first mark left the
    other tools' ✓ standing on the same missing run."""
    if not CASE_MARK.search(row):
        return row
    lead = row[:len(row) - len(row.lstrip())]
    s = row.strip()
    opened, closed = s.startswith("|"), s.endswith("|") and len(s) > 1
    cells = _cells(row)
    said = False
    for k, c in enumerate(cells):
        if (cols is not None and k not in cols) or not CASE_MARK.search(c):
            continue

        def sub(_m):
            nonlocal said
            if said:
                return "not tested"
            said = True
            return f"not tested ({why})"
        cells[k] = CASE_MARK.sub(sub, c)
    return lead + ("|" if opened else "") + "|".join(cells) + (
        "|" if closed else "")


def mark_untested_cells(text: str, rows: Optional[list[dict]]) -> tuple[str, int]:
    """Matrix rows that cite no run that reached the code get ``not
    tested`` in every marked cell; ``(text, changed)``. ``rows=None``
    (no shell) changes nothing."""
    if rows is None:
        return text, 0
    ran = {r["n"] for r in rows if r.get("ran")}
    lines = text.split("\n")
    n = 0
    for it in matrix_rows(text):
        if cited(it["text"]) & ran:
            continue
        why = ("read, not run" if _CODE_CITE.search(it["text"])
               else "no run cited")
        lines[it["start"]] = not_tested(lines[it["start"]], why)
        n += 1
    return "\n".join(lines), n


def claim_audit_items(text: str, rows: Optional[list[dict]]) -> list[dict]:
    """Ranked items, then matrix cells, citing runs that reached the
    code, each with those runs (``runs``): at most
    :data:`CLAIM_AUDIT_MAX_ITEMS` ranked and :data:`MATRIX_MAX_ITEMS`
    cells. A cell is audited on its own (``col``, ``header``,
    ``cell``) against the runs it cites, else the runs its row cites."""
    if not rows:
        return []
    by_n = {r["n"]: r for r in rows if r.get("ran")}

    def runs_of(t):
        return [by_n[n] for n in sorted(cited(t)) if n in by_n][
            :CLAIM_AUDIT_RUNS_PER_ITEM]
    ranked = []
    for it in ranked_items(text):
        runs = runs_of(it["text"])
        if runs:
            ranked.append(dict(it, runs=runs))
    cells = []
    for it in matrix_rows(text):
        row_runs = runs_of(it["text"])
        if not row_runs:
            continue
        for col, header, cell in it["cols"]:
            cells.append(dict(it, col=col, header=header, cell=cell,
                              runs=runs_of(cell) or row_runs))
    return ranked[:CLAIM_AUDIT_MAX_ITEMS] + cells[:MATRIX_MAX_ITEMS]


def _run_block(r: dict) -> str:
    return (f"[{r['id']}] `{_norm(r['command'])}` → {r['exit']}\n"
            f"{_shown_output(r)}")


def _meaning(cell: str, legend: dict) -> str:
    m = CASE_MARK.search(cell)
    mark = m.group(0)[0] if m else ""
    return legend.get(mark) or MARK_MEANING.get(mark, f"is marked {cell!r}")


def _claim_text(it: dict) -> str:
    claim = re.sub(r"\s+", " ", it["text"]).strip()
    if it.get("kind") == "cell":
        case = re.sub(r"\*\*|__", "", _cells(it["text"])[0]).strip() or "this case"
        tool = it["header"].strip("` ")
        note = re.sub(r"\s+", " ", it.get("note") or "").strip()
        claim = (f"{tool} on {case}: {_meaning(it['cell'], it.get('legend') or {})}"
                 f" (the answer's mark: {it['cell']!r}"
                 + (f"; its note for this case: {note}" if note else "") + ")")
    return claim


def claim_audit_prompt(items: list[dict]) -> str:
    """RUN OUTPUTS, each cited run once and whole, then the claims
    ``A1``… naming the runs they cite (run 6: a run cited by twelve
    cells was rendered twelve times, so each copy was cut at 1,500
    characters)."""
    runs: dict[str, dict] = {}
    for it in items:
        for r in it["runs"]:
            runs.setdefault(r["id"], r)
    order = sorted(runs.values(), key=lambda r: r.get("n", 0))
    head = "RUN OUTPUTS (each run once, its whole output):\n\n" + \
        "\n\n".join(_run_block(r) for r in order)
    claims = "\n\n".join(
        f"A{k}. CLAIM: {_claim_text(it)}\nCITED RUNS: "
        + ", ".join(r["id"] for r in it["runs"])
        for k, it in enumerate(items, start=1))
    return head + "\n\n=== CLAIMS ===\n\n" + claims


def claim_audit_batches(items: list[dict], budget_chars: int) -> list[list[int]]:
    """Item indexes in groups whose prompt fits ``budget_chars``: an item
    joins a group while its claim and the runs the group does not yet
    hold fit; otherwise it starts the next. A run is never cut: an item
    whose own runs exceed the budget is a group of its own."""
    groups: list[list[int]] = []
    held: set[str] = set()
    used = 0
    for i, it in enumerate(items):
        new = [r for r in it["runs"] if r["id"] not in held]
        cost = len(_claim_text(it)) + 40 + sum(len(_run_block(r)) + 2
                                                for r in new)
        if groups and used + cost <= budget_chars:
            groups[-1].append(i)
        else:
            groups.append([i])
            held, used = set(), 0
            new = it["runs"]
            cost = len(_claim_text(it)) + 40 + sum(len(_run_block(r)) + 2
                                                    for r in new)
        held.update(r["id"] for r in new)
        used += cost
    return groups


#: A ledger finding id in an answer (``csf-2``, ``(known: csf-2)``).
FINDING_TAG = re.compile(r"(?<![\w-])([a-z]{1,6}f-\d+)(?![\w-])")
RECITED_NOTE = "harness: shown by {ids}, not by the cited {old}"


def second_look_items(items: list[dict], verdicts: dict[int, tuple[str, str]],
                      rows: Optional[list[dict]]) -> list[tuple[int, dict]]:
    """``[(k, item)]`` for each ranked item (1-based ``k``) found ``not
    shown`` or ``partly`` that names a ledger finding: the same item with
    ``runs`` = other runs that reached the code and name that finding
    (tests and probes first, newest first). Council run 8: csf-2 cited
    C419, which tests grep only, while the coder's C280 ran csf-2 red to
    green."""
    ran = [r for r in rows or [] if r.get("ran")]
    out = []
    for k, it in enumerate(items, start=1):
        v = verdicts.get(k)
        if it.get("kind") == "cell" or not v or v[0] not in ("not shown", "partly"):
            continue
        tags = set(FINDING_TAG.findall(it["text"]))
        if not tags:
            continue
        have = {r["id"] for r in it["runs"]}
        pats = [re.compile(rf"(?<![\w-]){re.escape(t)}(?![\w-])") for t in tags]
        cands = [r for r in ran if r["id"] not in have and any(
            p.search(f"{r.get('command') or ''}\n{r.get('output') or ''}")
            for p in pats)]
        cands.sort(key=lambda r: (not is_probe(r.get("command") or ""),
                                  -int(r.get("n") or 0)))
        if cands:
            out.append((k, dict(it, runs=cands[:CLAIM_AUDIT_RUNS_PER_ITEM],
                                recite_from=[r["id"] for r in it["runs"]])))
    return out


BRIEF_OWED_NOTE = ("{id}: the checkpoint brief named it, and no run after "
                   "the brief that reached the code names it — not "
                   "re-verified (harness)")


def brief_owed(text: str, docs: list, rows: Optional[list[dict]],
               ruled_out: Iterable[str] = ()) -> str:
    """The answer with every finding an injected brief names, that no run
    after the brief (one that reached the code) mentions, listed under
    ``Unverified``. Council run 8: the brief asked for csf-8 on the
    patched tree; nothing ran it, and the answer did not say so.
    ``ruled_out`` (refuted / by_design ids) are never owed: a brief names
    them to say "do not re-raise"."""
    if rows is None or not docs:
        return text
    skip = {x.lower() for x in ruled_out}
    owed = []
    for d in docs:
        ids = list(dict.fromkeys(FINDING_TAG.findall(getattr(d, "text", "") or "")))
        after = [r for r in rows if r.get("ran") and (r.get("ts") or 0)
                 >= (getattr(d, "ts", 0) or 0)]
        for fid in ids:
            if fid.lower() in skip:
                continue
            rx = re.compile(rf"(?<![\w-]){re.escape(fid)}(?![\w-])")
            if fid in owed or any(rx.search(f"{r.get('command') or ''}\n"
                                            f"{r.get('output') or ''}")
                                  for r in after):
                continue
            owed.append(fid)
    lines = text.rstrip().split("\n")
    new = [f"- {BRIEF_OWED_NOTE.format(id=fid)}." for fid in owed
           if BRIEF_OWED_NOTE.format(id=fid) not in text]
    if not new:
        return text
    if not _insert_under(lines, UNVERIFIED_HEADING, new):
        lines += ["", "## Unverified", ""] + new
    return "\n".join(lines).rstrip() + "\n"


def parse_claim_audit(reply: str, n: int) -> dict[int, tuple[str, str]]:
    """``{k: (verdict, why)}`` for the lines that parse; ``k`` from 1."""
    out: dict[int, tuple[str, str]] = {}
    for m in _VERDICT_LINE.finditer(reply or ""):
        k = int(m.group(1))
        if 1 <= k <= n and k not in out:
            out[k] = (" ".join(m.group(2).lower().split()),
                      m.group(3).strip().strip("`")[:240])
    return out


def _with_note(lines: list[str], it: dict, note: str) -> None:
    """``note`` on a ranked item: in its last cell for a table row (a
    line after a row would break the table), else on a line after it."""
    last = it["end"] - 1
    if lines[it["start"]].strip().startswith("|"):
        row = lines[it["start"]].rstrip()
        cut = row[:-1].rstrip() if row.endswith("|") else row
        lines[it["start"]] = f"{cut} _({note})_ |"
        return
    indent = "   " if _ITEM.match(lines[it["start"]]) else ""
    lines.insert(last + 1, f"{indent}_({note})_")


def apply_claim_audit(text: str, items: list[dict],
                      verdicts: dict[int, tuple[str, str]]) -> tuple[str, int]:
    """The answer with every ranked item found ``not shown`` or
    ``contradicted`` moved to ``Unverified``, every ``partly`` one kept
    with what is not shown noted on it and listed under ``Unverified``,
    and every such matrix cell marked ``not tested``; ``(text,
    changed)``. An item with no verdict stays as it is."""
    lines = text.split("\n")
    moved = []
    cells = 0
    for k, it in enumerate(items, start=1):          # in place, first
        v = verdicts.get(k)
        if it.get("kind") == "cell" and v and v[0] != "shown":
            ids = ", ".join(r["id"] for r in it["runs"])
            lines[it["start"]] = not_tested(
                lines[it["start"]], f"audit: {v[0]} by {ids}",
                cols=[it["col"]] if "col" in it else None)
            cells += 1
    for k, it in sorted(enumerate(items, start=1), key=lambda x: -x[1]["start"]):
        v = verdicts.get(k)
        if it.get("kind") != "cell" and v and v[0] == "shown" \
                and it.get("recite_from"):
            _with_note(lines, it, RECITED_NOTE.format(
                ids=", ".join(r["id"] for r in it["runs"]),
                old=", ".join(it["recite_from"])))
            moved.append(None)
            continue
        if it.get("kind") == "cell" or not v or v[0] == "shown":
            continue
        ids = ", ".join(r["id"] for r in it["runs"])
        why = v[1] or "no reason given"
        if v[0] == "partly":
            _with_note(lines, it, PARTLY_NOTE.format(ids=ids, why=why))
            moved.insert(0, "- " + PARTLY_UNVERIFIED.format(
                label=_label(it["text"]), ids=ids, why=why.rstrip(".")))
            continue
        moved.insert(0, f"- {_label(it['text'])}: " + NOT_SHOWN_NOTE.format(
            verdict="not shown" if v[0] == "not shown" else "contradicted",
            ids=ids, why=why) + ".")
        del lines[it["start"]:it["end"]]
    recited = sum(1 for m in moved if m is None)
    moved = [m for m in moved if m is not None]
    if not moved:
        if cells or recited:
            return "\n".join(lines), cells + recited
        return text, 0
    if not _insert_under(lines, UNVERIFIED_HEADING, moved):
        lines += ["", "## Unverified", ""] + moved
    return "\n".join(lines).rstrip() + "\n", len(moved) + cells + recited


# ------------------------------------------------- the critic and the ledger
CRITIC_LEDGER_NOTE = (
    "Check every claim a report marks verified, confirmed or reproduced "
    "against that lane's own runs below. A claim its own output "
    "contradicts is refuted: say so, and name the run. A test that only "
    "prints passes whatever the code does; read what it printed. The "
    "label a report gives a case (BLIND, refused, accepted, ✓, ⚠) is a "
    "claim too: check it against what that run printed, and name every "
    "label the output does not bear out (council run 5: an edit of a line "
    "the run showed read and unchanged was called BLIND).")


# ---------------------------------------------- held tests ("Sandbox changes")
TEST_FILE = _tr.TEST_FILE


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
    events = []
    for _eid, tool, args, output in rows:
        if tool == "write_file":
            try:
                wp = str((json.loads(args or "{}") or {}).get("path") or "")
            except ValueError:
                wp = ""
            hit = next((p for p in tests if wp and (
                wp == p or wp.endswith("/" + p) or p.endswith(wp))), None)
            if hit:
                events.append(("write", hit, ""))
            continue
        cmd = _command_of(args)
        for p in tests:          # a heredoc names the file by its short path
            if any(p.endswith(t) or t.endswith(p) for t in _tr.written_by(cmd)):
                events.append(("write", p, ""))
        events.append(("run", cmd, output or ""))
    found = _tr.classify(events)
    return {p: f"⚠ {why}" for p, why in found.items() if p in tests}


__all__ = ["hold_to_evidence_bar", "round_lanes", "claim_audit_items", "claim_audit_prompt",
           "claim_audit_batches",
           "parse_claim_audit", "apply_claim_audit", "CLAIM_AUDIT_SYSTEM",
           "matrix_rows", "mark_untested_cells", "thin_dropped", "no_run_report_check", "lane_runs", "commands_block", "commands_ran",
           "needs_unverified_revision", "with_unverified_stub",
           "audit_answer", "revision_note", "enforce", "Audit",
           "ranked_items", "probe_groups", "is_probe", "cited",
           "held_test_notes", "REVISION_NOTE", "BRIEF_ATTRIBUTION",
           "CRITIC_LEDGER_NOTE", "EVIDENCE_RULES", "TESTS_NOT_RUN"]
