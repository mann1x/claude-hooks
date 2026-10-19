"""Milestone C exit bench: fix-rate by compaction count (plan §3, Exit C).

A capable local model (qwen3.6 27B on eleven2go by default: a weak
model's failure says nothing about compaction), pinned to a 32,768-token
window) runs the researcher's production tool loop — the real
``_loop_payload_budgeter``, ``compaction.compact`` and the builtin
read-only tools — over a generated project that is several windows
long. Every trial therefore folds its history several times.

Task v2 (2026-10-14). Every fact the fix needs can be learned once only,
so it has to survive the folds — v1 let the model re-read the file it had
lost, and ``basic`` (no model call) scored as well as the agentic flows:

- ``src/config/limits.py`` (read first) gives the retry window in
  **deciseconds** (``RETRY_WINDOW = 450``); nothing else in the project
  says so, and "ms vs s" intuition gives the wrong fix (×1000, not ×100).
- That file can be read **once**: later reads answer "locked by a deploy"
  (:class:`OnceReadable`).
- After ``FOLLOWUP_AFTER`` tool results the user adds, once, a typed
  follow-up: cite ticket ``LDG-4471``. Only the user-request block or the
  replay can carry it past a fold.

``src/net/backoff.py`` (read last) compares the window against an elapsed
time in **milliseconds**. A trial is *fixed* when the answer names the
function, the decisecond unit, a ×100 conversion and the ticket
(:func:`grade`, deterministic).

Arms are ``flow/placement`` pairs: ``reduced`` and ``full`` crossed with
``system`` and ``user`` (the §11.2 placement A/B), plus ``basic`` (the
deterministic fold, no model call) as the floor.

    python benchmarks/consultants/compaction_bench.py \\
        --base http://192.168.178.161:22434 \\
        --model qwen36-base-mtp_tb:27b-q4km-128k \\
        --arms reduced/system,reduced/user,full/system,full/user,basic/system \\
        --trials 4 --concurrency 2

Outputs under ``results/<date>/compaction/``: ``trials.jsonl`` (local),
``metadata.json`` and ``report.md`` (committed).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from claude_hooks.agent_loop.runner import LoopConfig, run_loop  # noqa: E402
from claude_hooks.caliber_proxy.tools import (  # noqa: E402
    make_executor, openai_tool_specs)
from claude_hooks.get_advice.chat_client import ChatClient  # noqa: E402
from consultants.engine import budget, compaction, council  # noqa: E402

WINDOW = 32_768
FORK_BASELINE = {0: 0.85, 1: 0.63, 2: 0.50, 3: 0.25}  # fixed by compactions

# --------------------------------------------------------------------------- #
# The project
# --------------------------------------------------------------------------- #

MODULES = [
    "config/limits", "config/env", "config/flags", "core/ids", "core/clock",
    "core/errors", "store/schema", "store/pages", "store/journal",
    "store/compactor", "store/index", "api/routes", "api/auth", "api/codec",
    "api/pagination", "jobs/queue", "jobs/worker", "jobs/scheduler",
    "jobs/locks", "metrics/counters", "metrics/export", "util/text",
    "util/paths", "util/hashing", "net/pool", "net/dns", "net/http",
    "net/backoff",
]

LIMITS_HEAD = '''"""Operational limits for ledgerd.

Every duration in this module is expressed in DECISECONDS (tenths of a
second), the unit the scheduler ticks in. Callers that measure time in
another unit must convert before comparing.
"""

#: How long a request may keep retrying before it gives up, in deciseconds.
RETRY_WINDOW = 450

#: Largest single backoff sleep, in deciseconds.
BACKOFF_CEILING = 80

#: Maximum journal segment size, in bytes.
SEGMENT_BYTES = 64 * 1024 * 1024
'''

BACKOFF_TAIL = '''

def should_retry(attempt: int, started_ns: int) -> bool:
    """Whether a failed request should be attempted again."""
    elapsed = (time.monotonic_ns() - started_ns) // 1_000_000
    if attempt >= MAX_ATTEMPTS:
        return False
    return elapsed < limits.RETRY_WINDOW


def next_delay(attempt: int) -> float:
    base = min(limits.BACKOFF_CEILING / 10, 0.25 * (2 ** attempt))
    return base * (0.5 + random.random() / 2)
'''

QUESTION = """Users report that ledgerd gives up on a failed request after the
very first retry, although retries are meant to continue for a while.

Audit the project under `src/`. Read EVERY module, one by one, in exactly
this order (do not skip any, do not read them out of order):

{order}

Then answer with:
1. **Root cause** — the file and the function that is wrong, and why.
2. **Fix** — the exact corrected line of code.
3. **Module table** — every module above with a one-line purpose.
"""

_WORDS = ("segment page record cursor batch shard token lease offset epoch "
          "buffer frame slot entry bucket window marker digest handle span "
          "cache queue mirror ledger").split()


def _filler(rng: random.Random, module: str, n_funcs: int) -> str:
    stem = module.split("/")[-1]
    out = []
    for i in range(n_funcs):
        a, b = rng.sample(_WORDS, 2)
        name = f"{a}_{b}_{i}"
        doc = " ".join(rng.choice(_WORDS) for _ in range(28))
        body = "\n".join(
            f"    {rng.choice(_WORDS)}_{j} = ({rng.choice(_WORDS)} or 0) "
            f"+ {rng.randint(1, 999)}  # {stem} step {j}"
            for j in range(9))
        out.append(f"\n\ndef {name}({a}, {b}=None):\n    \"\"\"{doc}.\"\"\"\n"
                   f"{body}\n    return {a}")
    return "".join(out)


def build_project(dest: Path, *, seed: int = 7, funcs: int = 7) -> list[str]:
    """Write the project; returns the reading order (paths under src/)."""
    rng = random.Random(seed)
    order = []
    for mod in MODULES:
        path = dest / "src" / f"{mod}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        head = f'"""ledgerd {mod.replace("/", ".")} module."""\n\nimport time\n'
        if mod == "config/limits":
            text = LIMITS_HEAD + _filler(rng, mod, funcs)
        elif mod == "net/backoff":
            text = (head + "import random\n\nfrom ledgerd.config import limits"
                    "\n\nMAX_ATTEMPTS = 6\n" + _filler(rng, mod, funcs)
                    + BACKOFF_TAIL)
        else:
            text = head + _filler(rng, mod, funcs)
        path.write_text(text, encoding="utf-8")
        order.append(f"src/{mod}.py")
    return order


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #

TASK_VERSION = 3
TICKET = "LDG-4471"
FOLLOWUP_AFTER = 8
FOLLOWUP = (f"One more thing from the on-call lead: whatever you find, the "
            f"answer must cite ticket {TICKET}, which tracks this incident.")
LOCKED = ("error: src/config/limits.py is locked by a running deploy and "
          "cannot be read again; use what you already know about it.")

#: A ×100 conversion between deciseconds and milliseconds, either side.
_CONVERSION = re.compile(
    r"RETRY_WINDOW\s*\*\s*100\b|100\s*\*\s*(limits\.)?RETRY_WINDOW"
    r"|elapsed\s*(//|/)\s*100\b|//\s*100[_,]?000[_,]?000\b"
    r"|//\s*1e8|45[_,]?000\b", re.I)


def grade(answer: str, order: list[str]) -> dict:
    text = answer or ""
    low = text.lower()
    names_fn = "should_retry" in text
    names_file = "backoff" in low
    units = "decisecond" in low or "tenths of a second" in low
    conversion = _CONVERSION.search(text) is not None
    ticket = TICKET in text
    modules = sum(1 for p in order if Path(p).stem in text)
    # An answer at all: a lane that stopped on a promise of more reading
    # ("I'll read the remaining 20 files") says nothing about compaction.
    answered = names_file or "root cause" in low
    return {"fixed": bool(names_fn and names_file and units and conversion
                          and ticket), "answered": answered,
            "names_fn": names_fn, "names_file": names_file, "units": units,
            "conversion": conversion, "ticket": ticket,
            "module_coverage": round(modules / len(order), 3)}


class OnceReadable:
    """The builtin executor, except ``src/config/limits.py`` answers once.

    Once it has been read, no other tool shows its lines either: a
    ``grep RETRY_WINDOW`` returned the "in deciseconds" comment and let a
    trial whose every fold dropped the fact re-learn it (task v3)."""

    def __init__(self, inner, path: str = "src/config/limits.py"):
        self.inner = inner
        self.path = path
        self.reads = 0
        self.refused = 0
        self.scrubbed = 0

    def __call__(self, name, args, cwd):
        if name == "read_file":
            try:
                target = (json.loads(args) if isinstance(args, str)
                          else dict(args or {})).get("path") or ""
            except (TypeError, ValueError, AttributeError):
                target = ""
            if str(target).lstrip("./").endswith(self.path):
                self.reads += 1
                if self.reads > 1:
                    self.refused += 1
                    return LOCKED
                return self.inner(name, args, cwd)
        out = self.inner(name, args, cwd)
        if self.reads and isinstance(out, str) and "limits.py" in out:
            kept = [ln for ln in out.split("\n")
                    if not re.match(r"^\S*config/limits\.py[:-]\d", ln)]
            if len(kept) != out.count("\n") + 1:
                self.scrubbed += 1
                kept.append("(src/config/limits.py is locked by a running "
                            "deploy; its lines are not shown)")
            out = "\n".join(kept)
        return out


def with_followup(prepare, *, after: int = FOLLOWUP_AFTER):
    """``prepare`` plus one typed user follow-up once ``after`` tool
    results are in the history — a typed prompt, not a harness note, so
    the cut planner treats it as the user's."""
    sent = {"done": False}
    #: generation -> whether that generation's marker carries the unit,
    #: read from the history each call is actually sent with.
    carried: dict[int, bool] = {}

    def wrapped(payload):
        msgs = payload.get("messages") or []
        if not sent["done"] and sum(1 for m in msgs
                                    if m.get("role") == "tool") >= after:
            msgs.append({"role": "user", "content": FOLLOWUP})
            payload["messages"] = msgs
            sent["done"] = True
        out = prepare(payload)
        for m in ((out if isinstance(out, dict) else payload).get("messages")
                  or []):
            if m.get("_compaction_marker"):
                text = str(m.get("content") or "").lower()
                carried[int(m.get("_compaction_generation") or 1)] = (
                    "decisecond" in text or "tenths of a second" in text)
        return out

    for attr in ("observe", "recover", "session"):
        if hasattr(prepare, attr):
            setattr(wrapped, attr, getattr(prepare, attr))
    wrapped.sent = sent
    wrapped.carried = carried
    return wrapped


# --------------------------------------------------------------------------- #
# One trial
# --------------------------------------------------------------------------- #

class PinnedClient(ChatClient):
    """A chat client whose window is pinned on both sides of the wire."""

    def __init__(self, base_url: str, window: int):
        super().__init__(base_url, timeout_s=900.0)
        self.window = window

    def context_length(self, model):  # noqa: D401 — planner side
        return self.window

    def chat(self, payload):
        payload = dict(payload)
        payload["options"] = {**(payload.get("options") or {}),
                              "num_ctx": self.window}
        return super().chat(payload)


class Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self.llm: list[dict] = []
        self.tools: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def record_event(self, *, kind, role, round, lane_idx, payload):
        with self._lock:
            self.events.append({"kind": kind, **payload})

    def record_llm(self, *, role, round, lane_idx, model, request, response,
                   prompt_tokens, completion_tokens, duration_ms, **_):
        with self._lock:
            self.llm.append({"role": role, "pt": prompt_tokens,
                             "ct": completion_tokens, "ms": duration_ms})

    def record_tool(self, name, args):
        path = ""
        try:
            path = (json.loads(args or "{}") or {}).get("path") or ""
        except (TypeError, ValueError, AttributeError):
            pass
        with self._lock:
            self.tools.append((name, path))


def run_trial(*, base: str, model: str, arm: str, idx: int, project: Path,
              order: list[str], max_iterations: int, think: bool) -> dict:
    flow, placement = arm.split("/")
    client = PinnedClient(base, WINDOW)
    rec = Recorder()
    knobs = budget.Knobs(compaction=compaction.Settings(flow=flow,
                                                        placement=placement))
    budgeter = council._loop_payload_budgeter(
        client, model, role="researcher", knobs=knobs, recorder=rec,
        round=0, lane_idx=idx)
    prepare = with_followup(budgeter)
    cfg = LoopConfig(max_iterations=max_iterations,
                     force_answer_after=max_iterations - 2,
                     tools_available=True, think=think,
                     force_first_tool_call=False, prepare_payload=prepare,
                     on_response=budgeter.observe)
    question = QUESTION.format(order="\n".join(f"{i + 1}. `{p}`"
                                               for i, p in enumerate(order)))
    payload = {"model": model, "stream": False, "think": think,
               "messages": [{"role": "system", "content":
                             council.RESEARCHER_SYSTEM},
                            {"role": "user", "content": question}]}

    def on_iter(_i, req, resp, ms):
        pt, ct = council._usage_from(resp)
        rec.record_llm(role="researcher", round=0, lane_idx=idx, model=model,
                       request=req, response=resp, prompt_tokens=pt,
                       completion_tokens=ct, duration_ms=ms)

    executor = OnceReadable(make_executor((str(project),)))
    t0 = time.monotonic()
    error = None
    answer = ""
    final = None
    try:
        final = run_loop(payload, str(project), config=cfg,
                         tool_specs=openai_tool_specs(),
                         chat_fn=budgeter.recover(client.chat),
                         tool_executor=executor, on_iter=on_iter,
                         on_tool=lambda name, args, *_: rec.record_tool(
                             name, args))
        answer = council._extract_text(final)
    except Exception as e:  # a failed trial is a data point, not a crash
        error = f"{type(e).__name__}: {e}"
    folds = [e for e in rec.events if e.get("kind") == "compaction"]
    calls = [c for c in rec.llm if c["role"] == "researcher"]
    comp_calls = [c for c in rec.llm if c["role"].endswith(":compaction")]
    reads = [p for n, p in rec.tools if n == "read_file" and p]
    distinct = set(reads)
    return {
        "arm": arm, "trial": idx, "model": model, "window": WINDOW,
        "error": error, "seconds": round(time.monotonic() - t0, 1),
        "compactions": len(folds),
        "fold_reports": [{k: f.get(k) for k in
                          ("flow", "placement", "reason", "before", "after",
                           "target", "trigger", "rescue", "calls",
                           "invalid_citations", "digest", "cut")} for f in folds],
        "researcher_calls": len(calls), "tool_calls": len(rec.tools),
        "reads": len(reads), "distinct_reads": len(distinct),
        "re_reads": len(reads) - len(distinct),
        "read_order": reads,
        "compaction_calls": len(comp_calls),
        "compaction_seconds": round(sum(c["ms"] for c in comp_calls) / 1000, 1),
        "max_prompt_tokens": max((c["pt"] or 0 for c in calls), default=0),
        "followup_sent": prepare.sent["done"],
        #: per generation, in order: did that digest carry the unit?
        "unit_carried": [prepare.carried[g] for g in sorted(prepare.carried)],
        "limits_reads": executor.reads, "limits_refused": executor.refused,
        "limits_scrubbed": executor.scrubbed,
        "stop_reason": (final.get("_stop_reason") if error is None
                        and isinstance(final, dict) else None),
        "answer": answer, **grade(answer, order),
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def _rate(rows):
    return (sum(r["fixed"] for r in rows) / len(rows)) if rows else None


def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def render_report(trials: list[dict], meta: dict) -> str:
    arms = list(dict.fromkeys(t["arm"] for t in trials))
    lines = [f"# Compaction bench — {meta['date']}", "",
             f"Model `{meta['model']}` at `{meta['base']}`, window pinned to "
             f"{WINDOW:,} tokens, think={meta['think']}, "
             f"{meta['trials']} trials per arm, commit `{meta['commit']}`.",
             "", "## By arm", "",
             "| arm | n | answered | fixed (of answered) | "
             "compactions (mean / min–max) | "
             "fold calls | fold s / trial | files read | re-reads | "
             "coverage | errors |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        rows = [t for t in trials if t["arm"] == arm]
        cs = [r["compactions"] for r in rows]
        answered = [r for r in rows if r.get("answered", True)]
        lines.append(
            f"| {arm} | {len(rows)} | {len(answered)} | "
            f"{_pct(_rate(answered))} ({sum(r['fixed'] for r in answered)}) | "
            f"{sum(cs) / len(cs):.1f} / {min(cs)}–{max(cs)} | "
            f"{sum(r['compaction_calls'] for r in rows) / len(rows):.1f} | "
            f"{sum(r['compaction_seconds'] for r in rows) / len(rows):.0f} | "
            f"{sum(r['distinct_reads'] for r in rows) / len(rows):.1f}"
            f"/{len(MODULES)} | "
            f"{sum(r['re_reads'] for r in rows) / len(rows):.1f} | "
            f"{sum(r['module_coverage'] for r in rows) / len(rows):.2f} | "
            f"{sum(1 for r in rows if r['error'])} |")
    lines += ["", "## Fix-rate by compaction count", "",
              "| compactions | fork baseline | " + " | ".join(arms) + " |",
              "|---|---|" + "---|" * len(arms)]
    buckets = sorted({min(t["compactions"], 3) for t in trials} | {0, 1, 2, 3})
    for b in buckets:
        cells = []
        for arm in arms:
            rows = [t for t in trials if t["arm"] == arm
                    and min(t["compactions"], 3) == b]
            cells.append(f"{_pct(_rate(rows))} ({len(rows)})")
        label = f"{b}+" if b == 3 else str(b)
        lines.append(f"| {label} | {_pct(FORK_BASELINE.get(b))} | "
                     + " | ".join(cells) + " |")
    lines += ["", "## Placement A/B (user vs system)", ""]
    for flow in ("reduced", "full"):
        s = [t for t in trials if t["arm"] == f"{flow}/system"]
        u = [t for t in trials if t["arm"] == f"{flow}/user"]
        if s and u:
            lines.append(f"- **{flow}**: system {_pct(_rate(s))} "
                         f"({len(s)}), user {_pct(_rate(u))} ({len(u)})")
    lines += ["", "## Facts carried", "",
              "| arm | decisecond unit | ×100 fix | ticket (follow-up) | "
              "limits.py re-reads refused |", "|---|---|---|---|---|"]
    for arm in arms:
        rows = [t for t in trials if t["arm"] == arm]
        lines.append(
            f"| {arm} | {sum(r.get('units', False) for r in rows)}/{len(rows)} | "
            f"{sum(r.get('conversion', False) for r in rows)}/{len(rows)} | "
            f"{sum(r.get('ticket', False) for r in rows)}/{len(rows)} | "
            f"{sum(r.get('limits_refused', 0) for r in rows)} |")
    lines += ["", "## Grader", "",
              f"Task v{TASK_VERSION}. A trial is fixed when the answer names "
              "`should_retry` and `backoff`, the decisecond unit, a ×100 "
              f"conversion and ticket `{TICKET}`. Each fact is available "
              "once (see the module docstring).", ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://192.168.178.161:22434")
    p.add_argument("--model", default="qwen36-base-mtp_tb:27b-q4km-128k")
    p.add_argument("--arms", default="reduced/system,reduced/user,"
                                     "full/system,full/user,basic/system")
    p.add_argument("--trials", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--max-iterations", type=int, default=48)
    p.add_argument("--think", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args(argv)

    date = dt.date.today().isoformat()
    out = args.out or (ROOT / "benchmarks/consultants/results" / date
                       / f"compaction-v{TASK_VERSION}")
    out.mkdir(parents=True, exist_ok=True)
    trials_path = out / "trials.jsonl"
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    meta = {"date": date, "task_version": TASK_VERSION,
            "model": args.model, "base": args.base,
            "window": WINDOW, "think": args.think, "trials": args.trials,
            "arms": args.arms.split(","), "commit": commit,
            "max_iterations": args.max_iterations,
            "fork_baseline": FORK_BASELINE}

    if not args.report_only:
        project = out / "project"
        order = build_project(project)
        done = set()
        if trials_path.exists():
            for line in trials_path.read_text().splitlines():
                t = json.loads(line)
                done.add((t["arm"], t["trial"]))
        jobs = [(arm, i) for i in range(args.trials) for arm in meta["arms"]
                if (arm, i) not in done]
        lock = threading.Lock()
        with cf.ThreadPoolExecutor(args.concurrency) as pool:
            futs = {pool.submit(run_trial, base=args.base, model=args.model,
                                arm=arm, idx=i, project=project, order=order,
                                max_iterations=args.max_iterations,
                                think=args.think): (arm, i)
                    for arm, i in jobs}
            for fut in cf.as_completed(futs):
                t = fut.result()
                with lock, trials_path.open("a") as f:
                    f.write(json.dumps(t) + "\n")
                print(f"{t['arm']:15s} #{t['trial']} fixed={t['fixed']} "
                      f"folds={t['compactions']} calls={t['researcher_calls']}"
                      f" {t['seconds']}s err={t['error']}", flush=True)
    trials = [json.loads(x) for x in trials_path.read_text().splitlines()]
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    (out / "report.md").write_text(render_report(trials, meta))
    print(out / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
