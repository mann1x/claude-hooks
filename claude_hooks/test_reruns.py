"""Test files a lane wrote that never ran green (council run 3, mail
#1633).

coder-0 wrote a 499-line test file with ``write_file`` and never ran it;
10 of its 16 tests failed on their own harness errors. researcher-101
wrote three files and ran each in the same command; all three failed to
load (a wrong import depth), and the lane reported anyway. Both were
held as patches that looked like evidence.

:func:`unrun_tests` reads a lane's own conversation (its ``write_file``
and ``run_command`` calls and their results) and names each test file
whose last write was not followed by a run, or whose last run failed.
The agent loop asks the lane once, before it reports
(:data:`NUDGE`), to run them or to say why they do not run. The same
patterns back the consultants engine's "Sandbox changes" flags
(``consultants.engine.evidence.held_test_notes``), which read the
engine's record instead of the conversation.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Iterable

#: A test file by its name: ``test_x.py``, ``x.test.ts``, ``x_spec.rb``,
#: ``x_test.go``.
TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+[._-](?:test|spec)s?\.[^/]+"
                       r"|[^/]+_test\.[^/]+)$")
#: A command that runs tests or a build.
TEST_LIKE = re.compile(
    r"\b(?:vitest|jest|pytest|mocha|ava|tap|bun\s+(?:x\s+)?(?:--bun\s+)?"
    r"(?:test|vitest|jest)|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|"
    r"yarn\s+test|cargo\s+(?:test|check|build)|go\s+(?:test|vet|build)|"
    r"make\s+(?:test|check)|ctest|tsc|unittest|node\s+--test|deno\s+test)"
    r"(?![.\w/-])",          # not ``vitest.config.ts`` / ``jest/``
    re.I)
#: A shell redirection or ``tee`` writing a file.
_WRITE_TARGET = re.compile(
    r"(?:>>?|\btee(?:\s+-a)?)\s*['\"]?([^\s'\";|&<>()]+)")
_EXIT = re.compile(r"^exit=(\S+)")

NUDGE = (
    "Before you report: these test files you wrote have not run green.\n"
    "{files}\n"
    "Run each one now. If it fails to load (an import path, a config, a "
    "harness error), fix the test, not the code under test, and run it "
    "again. If it cannot run here, say so in your report and why. A test "
    "that was never run, or does not load, is not evidence, and it is held "
    "for the user as if it were.")


def exit_of(output: str) -> str:
    """``exit 0`` / ``exit 1`` / ``harness error`` / ``?`` from a
    ``run_command`` result."""
    head = (output or "").lstrip().split("\n", 1)[0]
    m = _EXIT.match(head)
    if m:
        return f"exit {m.group(1)}"
    if (output or "").lstrip().startswith("error"):
        return "harness error"
    return "?"


#: A test run that reports failures, whatever its exit code says (run 4:
#: lane 102 piped vitest through ``| tail``, exit 0, 11 tests failing).
TEST_FAILED = re.compile(r"\b[1-9]\d*\s+(?:tests?\s+)?(?:failed|failing)\b|"
                         r"^\s*(?:FAIL|✗|×)\s", re.M)
_SEGMENT = re.compile(r"&&|\|\||[;|\n]")


def _copied_to(command: str) -> list[str]:
    """Targets of ``cp`` / ``mv`` / ``install`` that are test files (run 4:
    lane 102 wrote its test in /scratch, then copied it into place)."""
    out = []
    for seg in _SEGMENT.split(command or ""):
        words = [w.strip("'\"") for w in seg.split()]
        if not words or words[0] not in ("cp", "mv", "install"):
            continue
        args = [w for w in words[1:] if not w.startswith("-")]
        if len(args) < 2:
            continue
        target = args[-1]
        if TEST_FILE.search(target):
            out.append(target)
        else:          # copied into a directory under its own name
            for src in args[:-1]:
                name = PurePosixPath(src).name
                if TEST_FILE.search(name):
                    out.append(target.rstrip("/") + "/" + name)
    return out


def written_by(command: str) -> list[str]:
    """Test files a shell command writes (``cat > a.test.ts <<EOF``,
    ``tee``, ``cp`` / ``mv`` / ``install`` into place)."""
    return ([t for t in _WRITE_TARGET.findall(command or "") if TEST_FILE.search(t)]
            + _copied_to(command))


def run_result(output: str) -> str:
    """:func:`exit_of`, except that a run whose own report shows failing
    tests is a failure even when a pipe made it exit 0."""
    code = exit_of(output)
    if code.startswith("exit 0") and TEST_FAILED.search(output or ""):
        return "tests failed (exit 0 through a pipe)"
    return code


def _args(call: dict) -> dict:
    a = (call.get("function") or {}).get("arguments")
    if isinstance(a, dict):
        return a
    try:
        v = json.loads(a or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _command(args: dict) -> str:
    if args.get("command"):
        return str(args["command"])
    argv = args.get("argv")
    return " ".join(str(x) for x in argv) if isinstance(argv, list) else ""


def classify(events: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """``{path: why}`` for every test file whose last write was not
    followed by a run, or whose last run failed. ``events`` are
    ``(kind, target, result)`` in order: ``("write", path, "")`` or
    ``("run", command, output)``."""
    last: dict[str, list] = {}      # path -> exits of runs since its last write
    for kind, target, result in events:
        if kind == "write":
            if TEST_FILE.search(target or ""):
                last[target] = []
            continue
        cmd = target or ""
        for p in written_by(cmd):
            last[p] = []
        if not TEST_LIKE.search(cmd):
            continue
        for p in last:
            name = PurePosixPath(p).name
            if re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", cmd) \
                    or p in cmd:
                last[p].append(run_result(result))
    out = {}
    for p, runs in last.items():
        if not runs:
            out[p] = "never run after it was written"
        elif not runs[-1].startswith("exit 0"):
            out[p] = f"its last run failed ({runs[-1]})"
    return out


def unrun_tests(messages: list[dict]) -> dict[str, str]:
    """:func:`classify` over a lane's conversation."""
    calls: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    results: dict[str, str] = {}
    for m in messages or []:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name") or ""
                args = _args(tc)
                if name == "write_file" and args.get("path"):
                    ev = ("write", str(args["path"]))
                elif name == "run_command":
                    ev = ("run", _command(args))
                else:
                    continue
                cid = tc.get("id") or f"_{len(order)}"
                calls[cid] = ev
                order.append(cid)
        elif m.get("role") == "tool" and m.get("tool_call_id") in calls:
            c = m.get("content")
            results[m["tool_call_id"]] = c if isinstance(c, str) else json.dumps(c)
    return classify((calls[c][0], calls[c][1], results.get(c, ""))
                    for c in order)


NOTHING_WRITTEN = (
    "[SYSTEM MESSAGE] {turn} turns in, you have written nothing. Writing is "
    "your job: write your first test file now, with what you already know, "
    "and run it. If the toolchain does not start, read the run_command "
    "description (vitest needs --configLoader runner here) instead of "
    "working around it.")


def wrote_anything(messages: list[dict]) -> bool:
    """Whether a conversation has a ``write_file`` call, or a shell command
    that writes a file (``cat > …``, ``tee``, ``cp`` / ``mv`` into place)."""
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name") or ""
            if name == "write_file":
                return True
            if name == "run_command":
                cmd = _command(_args(tc))
                if _WRITE_TARGET.search(cmd) or _copied_any(cmd):
                    return True
    return False


def _copied_any(command: str) -> bool:
    for seg in _SEGMENT.split(command or ""):
        words = seg.split()
        if words and words[0] in ("cp", "mv", "install") and len(
                [w for w in words[1:] if not w.startswith("-")]) >= 2:
            return True
    return False


def nothing_written_check(at: int):
    """A ``LoopConfig.turn_check`` for a role whose job is writing (the
    coder): from turn ``at`` on, a note when it has written nothing."""
    def check(iteration: int, messages: list) -> "str | None":
        if iteration + 1 < at or wrote_anything(messages):
            return None
        return NOTHING_WRITTEN.format(turn=iteration)
    return check


def nudge(found: dict[str, str]) -> str:
    return NUDGE.format(files="\n".join(f"- `{p}`: {why}"
                                        for p, why in sorted(found.items())))


__all__ = ["TEST_FILE", "TEST_LIKE", "TEST_FAILED", "classify", "unrun_tests",
           "nudge", "exit_of", "run_result", "written_by", "NUDGE",
           "wrote_anything", "nothing_written_check", "NOTHING_WRITTEN"]
