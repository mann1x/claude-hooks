"""M-B bench — does giving every role tools actually buy anything?

``[tools] all_roles`` lets planner / critic / meta_critic / synthesizer /
adversary call the same tools the researcher has. The code is cheap; the
question is whether it is worth its cost, and that is what this measures.

Why the question is not rhetorical: ``tool_executor`` passed its M11c
bench at 87.5% pass / 5.00 quality, was defaulted on, and the live A/B
flipped it straight back off at +12 minutes wall and +43% tokens on a
real question. A bench that only measures the upside reproduces exactly
that mistake, so this one measures both halves.

Two tiers, mirroring M11a
=========================

**Tier 1 — detection (the value half).** Drive ``critic_node`` alone
against a synthetic fixture cohort, handing it research text with
*planted* claims: some verifiable, some fabricated. Ground truth is
known because we planted it. One critic invocation per trial, so the
signal is isolated from council noise and cheap enough to run wide.

Scored on both axes deliberately::

    recall     = planted-false claims the critic flagged
    precision  = of everything it flagged, how much was actually false

Recall alone would reward a critic that flags everything, and a council
whose critic cries wolf is worse than one that stays quiet. ``medium-02``
is the control: every claim in it is true, so any flag there is a false
positive.

**Tier 2 — cost (the other half).** The same question through a full
council with ``all_roles`` off, then on. Tokens and wall clock. This is
the arm that decides the default, because the multiplier is roles ×
lanes × iterations and only a real run exposes it.

The oracle is keyword-based, and that is a deliberate limitation
=================================================================
A planted claim is scored as "caught" when the critic's verdict text
mentions the fabricated token near a doubt word. No LLM judge: the
signal we need is binary and objective, a judge would cost more than
the trials, and a judge's own hallucination would be indistinguishable
from the effect being measured. The cost is that a critic which
*describes* the problem without naming the token scores as a miss — so
Tier 1 recall is a **lower bound**, and ``hard-02`` sits near the edge
of what this can score at all. Read the transcripts before trusting a
close call.

Usage::

    # Tier 1, dry run — no LLM, proves the harness wiring
    python -m benchmarks.consultants.role_tools_bench --tier detect --dry-run

    # Tier 1 live
    python -m benchmarks.consultants.role_tools_bench --tier detect \\
        --live --model gemma4:31b-cloud --trials 3

    # Tier 2 cost A/B against a running daemon
    python -m benchmarks.consultants.role_tools_bench --tier cost \\
        --live --question "..." --effort medium
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BENCH_VERSION = "1.0"
SUITE_DIR = Path(__file__).resolve().parent / "questions" / "role_tools"

#: Words that mark a claim as doubted rather than merely repeated. A
#: critic that echoes "retry_state.py" while agreeing with it has not
#: caught anything, so proximity to one of these is what counts.
_DOUBT_WORDS = (
    "unverified", "unsubstantiated", "cannot verify", "could not verify",
    "unable to verify", "not verify", "no such", "does not exist",
    "doesn't exist", "not exist", "not found", "no evidence", "fabricat",
    "incorrect", "wrong", "inaccurate", "mismatch", "actually",
    "unsupported", "unfounded", "questionable", "dubious", "flag",
    "not present", "missing", "false", "contradict", "but the",
)

#: How far from the token a doubt word may sit and still count.
_DOUBT_WINDOW = 240


# ====================================================================== #
# Corpus
# ====================================================================== #
@dataclass
class DetectQuestion:
    """One Tier-1 item: research text plus what is true and false in it."""

    id: str
    tier: str
    fixtures_subdir: str
    task: str
    research: str
    planted_false: tuple[str, ...]
    planted_true: tuple[str, ...]
    #: Tokens that appear ONLY in the correct version of a claim — the
    #: true value the research got wrong (``15`` where it said ``5``),
    #: or the true line (``retry.py:14`` where it said ``:1``). Their
    #: presence in a verdict is proof the critic looked and got the
    #: right answer; whether it then *said so* is the silent-correction
    #: measurement. Empty for questions whose falsehood is a pure
    #: non-existence, where there is no corrected value to name.
    correction_tokens: tuple[str, ...] = ()

    @property
    def is_control(self) -> bool:
        """No planted falsehood — any flag here is a false positive."""
        return not self.planted_false


_SECTION_RE = re.compile(r"^##\s+([A-Z_]+)\s*$", re.MULTILINE)


def _sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    marks = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out[m.group(1)] = body[m.end():end].strip()
    return out


def suite_hash(suite_dir: Path = SUITE_DIR) -> str:
    """Content hash of the corpus, stamped onto every trial.

    Without it a results directory cannot answer "was this measured
    against the questions currently on disk?" — and a silently edited
    question turns an old number into a wrong one.
    """
    from benchmarks.consultants.harness import (
        _hash_for_suite,
        load_suite_manifest,
    )
    man = load_suite_manifest(suite_dir)
    return _hash_for_suite(suite_dir, list(man.manifest))


def load_detect_questions(suite_dir: Path = SUITE_DIR) -> list[DetectQuestion]:
    """Parse the corpus. Reuses the harness frontmatter parser so the
    suite-hash machinery keeps working on these files."""
    from benchmarks.consultants.harness import _parse_frontmatter

    qs: list[DetectQuestion] = []
    for path in sorted(suite_dir.glob("*.md")):
        if path.name == "SUITE.md":
            continue
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        sec = _sections(body)
        qs.append(DetectQuestion(
            id=meta.get("id") or path.stem,
            tier=meta.get("tier") or "easy",
            fixtures_subdir=meta.get("fixtures_subdir") or "",
            task=(meta.get("task") or "").strip(),
            research=sec.get("RESEARCH", "").strip(),
            planted_false=tuple(
                x.strip() for x in sec.get("PLANTED_FALSE", "").splitlines()
                if x.strip()),
            planted_true=tuple(
                x.strip() for x in sec.get("PLANTED_TRUE", "").splitlines()
                if x.strip()),
            correction_tokens=tuple(
                x.strip() for x in
                sec.get("CORRECTION_TOKENS", "").splitlines() if x.strip()),
        ))
    return qs


# ====================================================================== #
# Scoring
# ====================================================================== #
def flagged(verdict: str, token: str) -> bool:
    """Whether ``verdict`` doubts ``token``.

    Presence alone is not enough — a critic restating a claim it accepts
    would otherwise score as a catch. We require a doubt word within
    ``_DOUBT_WINDOW`` characters of an occurrence.
    """
    if not verdict or not token:
        return False
    low = verdict.lower()
    tok = token.lower()
    start = 0
    while True:
        i = low.find(tok, start)
        if i < 0:
            return False
        lo = max(0, i - _DOUBT_WINDOW)
        hi = min(len(low), i + len(tok) + _DOUBT_WINDOW)
        if any(w in low[lo:hi] for w in _DOUBT_WORDS):
            return True
        start = i + len(tok)


_CORRECTIONS_RE = re.compile(r"^\s*CORRECTIONS\s*:", re.MULTILINE)


def has_corrections_block(verdict: str) -> bool:
    """Whether the verdict declares corrections in the agreed shape.

    The v1.2 run showed the critic fetching the right line, silently
    substituting it, and calling the report accurate — the correction
    happened inside the model and never reached the synthesizer, which
    went on relaying the wrong value. Detecting the block is how we
    tell "it corrected and told us" from "it corrected and didn't".
    """
    if not verdict:
        return False
    # ``CORRECTIONS: none`` is explicitly not the contract — the block
    # is omitted when nothing was corrected — so treat it as absent
    # rather than crediting a model that wrote the header and no rows.
    for m in _CORRECTIONS_RE.finditer(verdict):
        tail = verdict[m.end():m.end() + 40].strip().lower()
        if not tail.startswith("none"):
            return True
    return False


def corrections_block_text(verdict: str) -> str:
    """The text of the CORRECTIONS block, or ``""``.

    Scoring needs this because the block is a *structured* declaration
    and the doubt-word heuristic cannot read it. A correct row reads
    "claimed `retry.py:1` — actual `retry.py:14`": no doubt word
    anywhere near the token, so the proximity oracle called three
    perfect catches misses. Inside the block, naming a claim IS
    doubting it — that is what the block means.
    """
    if not verdict:
        return ""
    m = _CORRECTIONS_RE.search(verdict)
    if not m:
        return ""
    tail = verdict[m.end():]
    if tail.strip().lower().startswith("none"):
        return ""
    return tail


@dataclass
class DetectTrial:
    """One (question × arm × trial_idx) Tier-1 result.

    ``arm`` is ``"untooled"`` or ``"tooled"`` — the whole point is the
    delta between them on identical input.
    """

    question_id: str
    tier: str
    arm: str
    trial_idx: int
    model: str = ""
    timestamp: str = ""
    suite_hash: str = ""
    bench_version: str = BENCH_VERSION
    error: Optional[str] = None
    # Outcome
    caught: list = field(default_factory=list)      # planted-false flagged
    missed: list = field(default_factory=list)      # planted-false not flagged
    false_positives: list = field(default_factory=list)  # planted-true flagged
    #: Looked it up, got the right answer, did not flag the wrong one.
    #: The failure the v1.3 directive pass targets: worse than a plain
    #: miss, because the evidence was in hand and thrown away.
    silent_fixes: list = field(default_factory=list)
    #: Verdict carried a CORRECTIONS block naming what it changed.
    corrections_reported: bool = False
    verdict_chars: int = 0
    #: The verdict itself. The oracle is keyword-based and undercounts,
    #: so the report tells the reader to check transcripts on a close
    #: call — which is only actionable if the transcript was kept. A
    #: scored run whose evidence was discarded cannot be audited later,
    #: and by then the tokens are spent.
    verdict: str = ""
    # Cost
    wall_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0

    @property
    def recall(self) -> Optional[float]:
        total = len(self.caught) + len(self.missed)
        return (len(self.caught) / total) if total else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recall"] = self.recall
        return d


# ====================================================================== #
# Tier 1 runner
# ====================================================================== #
class _StubChat:
    """Dry-run client — proves the wiring without an LLM.

    Deliberately returns a verdict that catches *nothing*, so a dry run
    can never be mistaken for a passing result. But it does issue one
    real ``grep`` on its first tooled turn, because the thing most worth
    proving offline is that the executor reaches the fixture directory
    and comes back with bytes. A dry run that skips the tool path leaves
    the interesting half untested.

    Serves both response shapes: ``choices`` for the agent loop, and the
    ``message`` key the single-shot path reads.
    """

    def __init__(self, text: str = "verdict: ready. No issues found.",
                 grep_pattern: str = "def "):
        self._text = text
        self._grep = grep_pattern
        self.calls = 0
        self.tool_turns = 0

    def chat(self, payload: dict) -> dict:
        self.calls += 1
        usage = {"prompt_tokens": 100, "completion_tokens": 40}
        wants_tools = bool(payload.get("tools"))
        if wants_tools and self.tool_turns == 0:
            self.tool_turns += 1
            call = {"id": "stub-1", "type": "function",
                    "function": {"name": "grep",
                                 "arguments": json.dumps(
                                     {"pattern": self._grep})}}
            return {"choices": [{"finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": "",
                                             "tool_calls": [call]}}],
                    "message": {"content": "", "tool_calls": [call]},
                    "usage": usage}
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": self._text}}],
                "message": {"content": self._text},
                "usage": usage}


def _live_chat(model: str, base_url: str):
    """The same factory the council uses, so ``llamafile://`` labels
    work here too and the bench cannot diverge from production by
    picking a different client."""
    from claude_hooks.get_advice.chat_client import make_agent_chat_client
    return make_agent_chat_client(model, base_url)


def run_detect_trial(q: DetectQuestion, *, arm: str, trial_idx: int,
                     chat_client, model: str,
                     suite_dir: Path = SUITE_DIR) -> DetectTrial:
    """Run ``critic_node`` once on ``q`` in the given arm."""
    from consultants.engine import council

    t = DetectTrial(question_id=q.id, tier=q.tier, arm=arm,
                    trial_idx=trial_idx, model=model,
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()))
    cwd = str(suite_dir / "fixtures" / q.fixtures_subdir)

    tool_specs = tool_executor = None
    n_tools = {"n": 0}
    if arm == "tooled":
        from claude_hooks.tool_registry import (
            BuiltinToolProvider,
            GitToolProvider,
            ToolRegistry,
        )
        reg = ToolRegistry([BuiltinToolProvider(), GitToolProvider()])
        tool_specs = reg.specs()
        raw = reg.as_executor()

        def counting(name, args, wd):
            n_tools["n"] += 1
            return raw(name, args, wd)

        tool_executor = counting

    state = {
        "question": q.task,
        "plan": "1. Verify the retry layer's bounds.",
        "research": [q.research],
        "research_rounds_used": 1,
    }
    t0 = time.monotonic()
    try:
        out = council.critic_node(
            state, chat_client=chat_client, model=model, think=False,
            tool_specs=tool_specs, tool_executor=tool_executor, cwd=cwd,
        )
    except Exception as e:
        t.error = f"{type(e).__name__}: {e}"
        t.wall_s = time.monotonic() - t0
        return t
    t.wall_s = time.monotonic() - t0
    t.tool_calls = n_tools["n"]

    # critic_node does not raise on failure — it tombstones, returning
    # ``critic_decision: ready`` with an error note so a live council
    # still produces an answer. For the bench that default is exactly
    # backwards: a tombstoned trial has an empty verdict, which the
    # oracle would score as a clean miss on every planted claim.
    # Surface it as an error so summarize() excludes it.
    if out.get("_role_failed") or out.get("error"):
        t.error = str(out.get("error") or "critic tombstoned")
        return t

    # ``critique`` is a string. Iterating it would join it character by
    # character and destroy every token the oracle looks for.
    critique = out.get("critique")
    verdict = critique if isinstance(critique, str) else " ".join(
        str(x) for x in (critique or []))
    if not verdict.strip():
        turns = out.get("turns") or []
        verdict = " ".join(getattr(x, "content", "") or "" for x in turns)
    t.verdict_chars = len(verdict)
    t.verdict = verdict
    for turn in (out.get("turns") or []):
        t.prompt_tokens += int(getattr(turn, "prompt_tokens", 0) or 0)
        t.completion_tokens += int(getattr(turn, "completion_tokens", 0) or 0)

    # A falsehood named inside the CORRECTIONS block is caught, and
    # caught through the *preferred* channel: the critic resolved it
    # rather than routing it back for another research round. Scoring
    # only the doubt-word phrasing would mark the intended behaviour a
    # failure and push the next directive pass the wrong way.
    block = corrections_block_text(verdict)
    for tok in q.planted_false:
        if flagged(verdict, tok) or tok.lower() in block.lower():
            t.caught.append(tok)
        else:
            t.missed.append(tok)
    for tok in q.planted_true:
        # A true claim quoted inside the block IS being contradicted,
        # so it counts against precision exactly as a flag would.
        if flagged(verdict, tok) or tok.lower() in block.lower():
            t.false_positives.append(tok)

    t.corrections_reported = has_corrections_block(verdict)
    # A silent fix requires evidence of BOTH halves: the correct value
    # is present (so it looked and got it right) and nothing was
    # flagged (so it kept that to itself). Missing the claim entirely
    # is a plain miss and is already counted as one.
    if t.missed and not t.corrections_reported:
        low = verdict.lower()
        for tok in q.correction_tokens:
            if tok.lower() in low:
                t.silent_fixes.append(tok)
    return t


# ====================================================================== #
# Report
# ====================================================================== #
def summarize(trials: list[DetectTrial]) -> dict:
    """Per-arm aggregates. Precision is computed over flags, not over
    questions, so the control question carries its full weight."""
    out: dict[str, dict] = {}
    for arm in ("untooled", "tooled"):
        rows = [t for t in trials if t.arm == arm and not t.error]
        caught = sum(len(t.caught) for t in rows)
        missed = sum(len(t.missed) for t in rows)
        fps = sum(len(t.false_positives) for t in rows)
        planted = caught + missed
        flags = caught + fps
        out[arm] = {
            "trials": len(rows),
            "errors": len([t for t in trials if t.arm == arm and t.error]),
            "planted_false": planted,
            "caught": caught,
            "recall": (caught / planted) if planted else None,
            "false_positives": fps,
            "precision": (caught / flags) if flags else None,
            "silent_fixes": sum(len(t.silent_fixes) for t in rows),
            "corrections_reported": sum(
                1 for t in rows if t.corrections_reported),
            "tool_calls": sum(t.tool_calls for t in rows),
            "prompt_tokens": sum(t.prompt_tokens for t in rows),
            "completion_tokens": sum(t.completion_tokens for t in rows),
            "wall_s": round(sum(t.wall_s for t in rows), 2),
        }
    return out


def render_report(summary: dict, trials: list[DetectTrial]) -> str:
    u, t = summary.get("untooled", {}), summary.get("tooled", {})

    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines = [
        "# M-B role-tools bench — Tier 1 (detection)",
        "",
        f"bench_version={BENCH_VERSION}  trials={len(trials)}",
        "",
        "| metric | untooled | tooled |",
        "|---|---|---|",
        f"| recall (planted falsehoods caught) | {pct(u.get('recall'))} "
        f"| {pct(t.get('recall'))} |",
        f"| precision (flags that were real) | {pct(u.get('precision'))} "
        f"| {pct(t.get('precision'))} |",
        f"| false positives | {u.get('false_positives')} "
        f"| {t.get('false_positives')} |",
        f"| silent fixes (looked, knew, didn't say) | "
        f"{u.get('silent_fixes')} | {t.get('silent_fixes')} |",
        f"| trials reporting CORRECTIONS | {u.get('corrections_reported')} "
        f"| {t.get('corrections_reported')} |",
        f"| tool calls | {u.get('tool_calls')} | {t.get('tool_calls')} |",
        f"| completion tokens | {u.get('completion_tokens')} "
        f"| {t.get('completion_tokens')} |",
        f"| wall (s) | {u.get('wall_s')} | {t.get('wall_s')} |",
        "",
        "Recall is a **lower bound**. A catch is scored two ways: the",
        "verdict names the fabricated token near a doubt word, or names",
        "it inside a `CORRECTIONS:` block (where naming a claim *is*",
        "disputing it). A critic that describes the problem without",
        "naming the token still reads as a miss. The block rule counts",
        "symmetrically — a *true* claim quoted inside it scores as a",
        "false positive — so it is not a one-way loosening.",
        "Read transcripts before acting on a close call.",
        "",
        "## Per question",
        "",
        "| question | arm | caught | missed | false pos | silent | corr |"
        " tools |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tr in trials:
        lines.append(
            f"| {tr.question_id} | {tr.arm} | {len(tr.caught)} "
            f"| {len(tr.missed)} | {len(tr.false_positives)} "
            f"| {len(tr.silent_fixes)} "
            f"| {'y' if tr.corrections_reported else '-'} "
            f"| {tr.tool_calls} |")
    return "\n".join(lines) + "\n"


# ====================================================================== #
# CLI
# ====================================================================== #
def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="role_tools_bench",
        description="M-B: what does giving every role tools buy?")
    p.add_argument("--tier", choices=("detect", "cost"), default="detect")
    p.add_argument("--live", action="store_true",
                   help="Call a real model. Without this the run is a "
                        "wiring check whose verdicts catch nothing.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default="gemma4:31b-cloud")
    p.add_argument("--ollama-base", default=os.environ.get(
        "OLLAMA_BASE_URL", "http://192.168.178.2:11433"))
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--arms", default="untooled,tooled")
    p.add_argument("--out", type=Path, default=None,
                   help="Directory for trials.jsonl + report.md")
    # --- Tier 2 ---
    p.add_argument("--question", default="",
                   help="Tier 2: the question both arms answer.")
    p.add_argument("--project", default="",
                   help="Tier 2: isolated dir holding the code under "
                        "discussion. Its .claude-hooks/consultants.toml "
                        "is REWRITTEN per arm, so never point this at a "
                        "real project you care about.")
    p.add_argument("--effort", default="high",
                   help="Tier 2: council effort tier (default high).")
    p.add_argument("--base", default="http://127.0.0.1:38095",
                   help="Tier 2: consultants engine base URL.")
    return p


def run_cost_arm(*, project: Path, question: str, all_roles: bool,
                 base: str, effort: str,
                 timeout_s: float = 3600.0) -> dict:
    """One full council run, and what it cost.

    The arm's config lives in ``project/.claude-hooks/consultants.toml``
    with ``override_user_global``, so flipping ``all_roles`` never
    touches the operator's real config. That is not tidiness — the
    engine reads the project layer for the request's cwd, so an A/B run
    against the live file would mutate the config of every other
    session on the host mid-flight, and a crashed run would leave it
    flipped.
    """
    import subprocess
    write_arm_config(project, all_roles=all_roles, effort=effort)
    # Point the CLI at the engine we were told to use. A long-lived
    # server caches modules, so an A/B against one started before the
    # change under test silently measures the old code — the reason
    # this is a parameter and not the CLI's default.
    env = dict(os.environ, CONSULTANTS_URL=base)
    t0 = time.monotonic()
    proc = subprocess.run(
        ["claude-consultants", "consult", "--wait", "--cwd", str(project),
         "--effort", effort, "--message", question],
        capture_output=True, text=True, timeout=timeout_s, env=env,
    )
    wall = time.monotonic() - t0
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "raw": (proc.stdout or "")[-2000:]}
    return {
        "all_roles": all_roles, "effort": effort, "wall_s": round(wall, 1),
        "ok": bool(payload.get("ok")), "sid": payload.get("sid"),
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-1000:],
        "payload": payload,
    }


def write_arm_config(project: Path, *, all_roles: bool,
                     effort: str) -> Path:
    """Write the isolated per-arm project config."""
    d = project / ".claude-hooks"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "consultants.toml"
    path.write_text(
        "# Generated by role_tools_bench --tier cost. Isolated from the\n"
        "# operator's user-global config on purpose; see run_cost_arm.\n"
        "override_user_global = true\n"
        'topology = "council"\n'
        f'effort = "{effort}"\n'
        "\n"
        "# Pin the pipeline to the roles the knob actually touches.\n"
        "# ``coder`` is left off deliberately: it is not a toolable role,\n"
        "# so it adds the same large cost to both arms — which does not\n"
        "# bias the delta but does inflate the denominator, making the\n"
        "# percentage look smaller than the effect on the roles the knob\n"
        "# governs. Its wall time and variance also dominate the run.\n"
        "# Per-role ``extra_models`` are NOT overridden, so the x-tier\n"
        "# fan-out (and the meta-critic it triggers) stays live — that\n"
        "# multiplier is the whole reason this arm exists.\n"
        "[role.planner]\nenabled = true\n"
        "[role.researcher]\nenabled = true\n"
        "[role.critic]\nenabled = true\n"
        "[role.synthesizer]\nenabled = true\n"
        "[role.coder]\nenabled = false\n"
        "[role.tool_executor]\nenabled = false\n"
        "\n[tools]\n"
        "enabled = true\n"
        f"all_roles = {'true' if all_roles else 'false'}\n",
        encoding="utf-8")
    return path


def cost_totals(sid: str, project: Path) -> dict:
    """Token + call totals for a finished run, read from its
    transcript.db — the engine's own record, not a re-derivation."""
    import sqlite3
    db = project / ".claude-hooks" / "consultants" / sid / "transcript.db"
    if not db.is_file():
        return {"error": f"transcript.db not found at {db}"}
    out: dict = {"by_role": {}}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # One `events` table discriminated by ``kind`` — not a table per
        # event type. See docs/consultants-transcript-db-schema.md.
        cur = con.execute(
            "SELECT role, COUNT(*), SUM(COALESCE(prompt_tokens,0)), "
            "SUM(COALESCE(completion_tokens,0)) FROM events "
            "WHERE kind = 'llm_call' GROUP BY role")
        for role, n, pt, ct in cur.fetchall():
            out["by_role"][role] = {"calls": n, "prompt_tokens": pt or 0,
                                    "completion_tokens": ct or 0}
        out["tool_calls"] = con.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'tool_call'"
        ).fetchone()[0]
        out["tool_calls_by_role"] = dict(con.execute(
            "SELECT role, COUNT(*) FROM events WHERE kind = 'tool_call' "
            "GROUP BY role").fetchall())
    finally:
        con.close()
    out["llm_calls"] = sum(v["calls"] for v in out["by_role"].values())
    out["prompt_tokens"] = sum(
        v["prompt_tokens"] for v in out["by_role"].values())
    out["completion_tokens"] = sum(
        v["completion_tokens"] for v in out["by_role"].values())
    return out


def render_cost_report(arms: list[dict]) -> str:
    def g(a, k, d=0):
        return (a.get("totals") or {}).get(k, d) or 0

    off = next((a for a in arms if not a["all_roles"]), {})
    on = next((a for a in arms if a["all_roles"]), {})

    def delta(k):
        a, b = g(off, k), g(on, k)
        return "n/a" if not a else f"{(b - a) / a * 100:+.0f}%"

    lines = [
        "# M-B Tier 2 — council cost A/B",
        "",
        f"effort={off.get('effort') or on.get('effort')}  "
        f"question hash={hashlib.sha256((off.get('question') or '').encode()).hexdigest()[:12]}",
        "",
        "| metric | all_roles off | all_roles on | delta |",
        "|---|---|---|---|",
        f"| LLM calls | {g(off,'llm_calls')} | {g(on,'llm_calls')} "
        f"| {delta('llm_calls')} |",
        f"| prompt tokens | {g(off,'prompt_tokens')} "
        f"| {g(on,'prompt_tokens')} | {delta('prompt_tokens')} |",
        f"| completion tokens | {g(off,'completion_tokens')} "
        f"| {g(on,'completion_tokens')} | {delta('completion_tokens')} |",
        f"| tool calls | {g(off,'tool_calls')} | {g(on,'tool_calls')} "
        f"| {delta('tool_calls')} |",
        f"| wall (s) | {off.get('wall_s')} | {on.get('wall_s')} |  |",
        "",
        "Per role (LLM calls / completion tokens):",
        "",
        "| role | off | on |",
        "|---|---|---|",
    ]
    roles = sorted(set((off.get("totals") or {}).get("by_role", {}))
                   | set((on.get("totals") or {}).get("by_role", {})))
    for r in roles:
        a = (off.get("totals") or {}).get("by_role", {}).get(r, {})
        b = (on.get("totals") or {}).get("by_role", {}).get(r, {})
        lines.append(
            f"| {r} | {a.get('calls', 0)} / {a.get('completion_tokens', 0)} "
            f"| {b.get('calls', 0)} / {b.get('completion_tokens', 0)} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    args = _argparser().parse_args(argv)
    if args.tier == "cost":
        if not args.question:
            print("--tier cost needs --question", file=sys.stderr)
            return 2
        if not args.project:
            print("--tier cost needs --project (an isolated dir holding "
                  "the code under discussion; its .claude-hooks/ config "
                  "is overwritten per arm)", file=sys.stderr)
            return 2
        project = Path(args.project).resolve()
        arms: list[dict] = []
        for all_roles in (False, True):
            if args.dry_run:
                path = write_arm_config(project, all_roles=all_roles,
                                        effort=args.effort)
                print(f"[dry-run] wrote {path} (all_roles={all_roles})")
                continue
            arm = run_cost_arm(project=project, question=args.question,
                               all_roles=all_roles, base=args.base,
                               effort=args.effort)
            arm["question"] = args.question
            if arm.get("sid"):
                arm["totals"] = cost_totals(arm["sid"], project)
            print(f"arm all_roles={all_roles}: ok={arm['ok']} "
                  f"wall={arm['wall_s']}s sid={arm.get('sid')}")
            if not arm["ok"]:
                print(f"  stderr: {arm['stderr'][-400:]}", file=sys.stderr)
            arms.append(arm)
        if args.dry_run:
            return 0
        report = render_cost_report(arms)
        print(report)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "report.md").write_text(report, encoding="utf-8")
            (args.out / "arms.json").write_text(
                json.dumps(arms, indent=2, default=str), encoding="utf-8")
            print(f"wrote {args.out}/report.md + arms.json")
        return 0

    questions = load_detect_questions()
    if not questions:
        print("no questions found", file=sys.stderr)
        return 1
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    shash = suite_hash()

    trials: list[DetectTrial] = []
    for q in questions:
        for arm in arms:
            for i in range(args.trials):
                if args.live and not args.dry_run:
                    client: Any = _live_chat(args.model, args.ollama_base)
                else:
                    client = _StubChat()
                tr = run_detect_trial(q, arm=arm, trial_idx=i,
                                      chat_client=client, model=args.model)
                tr.suite_hash = shash
                trials.append(tr)

    summary = summarize(trials)
    report = render_report(summary, trials)
    print(report)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        with (args.out / "trials.jsonl").open("w", encoding="utf-8") as fh:
            for t in trials:
                fh.write(json.dumps(t.to_dict()) + "\n")
        (args.out / "report.md").write_text(report, encoding="utf-8")
        (args.out / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {args.out}/trials.jsonl + report.md + summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
