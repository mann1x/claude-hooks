"""Milestone C exit bench: fix-rate by compaction count (plan §3, Exit C).

A small local model (``gemma4:e4b`` by default, pinned to a 32,768-token
window) runs the researcher's production tool loop — the real
``_loop_payload_budgeter``, ``compaction.compact`` and the builtin
read-only tools — over a generated project that is several windows
long. Every trial therefore folds its history several times.

The task plants one bug whose two halves sit at opposite ends of the
reading order: ``src/config/limits.py`` (read first) says the retry
window is in **seconds**; ``src/net/backoff.py`` (read last) compares it
against an elapsed time in **milliseconds**. The correct fix needs the
fact from the first file to have survived every fold. The grader is
deterministic (:func:`grade`); a trial is *fixed* when the answer names
the function, the unit mismatch and a correct conversion.

Arms are ``flow/placement`` pairs: ``reduced`` and ``full`` crossed with
``system`` and ``user`` (the §11.2 placement A/B), plus ``basic`` (the
deterministic fold, no model call) as the floor.

    python benchmarks/consultants/compaction_bench.py \\
        --base http://192.168.178.2:11433 --model gemma4:e4b \\
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
from typing import Any, Optional

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

Every duration in this module is expressed in SECONDS. Callers that
measure time in another unit must convert before comparing.
"""

#: How long a request may keep retrying before it gives up, in seconds.
RETRY_WINDOW = 45

#: Largest single backoff sleep, in seconds.
BACKOFF_CEILING = 8

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
    base = min(limits.BACKOFF_CEILING, 0.25 * (2 ** attempt))
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

_CONVERSION = re.compile(
    r"RETRY_WINDOW\s*\*\s*1[_,]?000|1[_,]?000\s*\*\s*(limits\.)?RETRY_WINDOW"
    r"|//\s*1[_,]?000[_,]?000[_,]?000|/\s*1e9|elapsed\s*/\s*1[_,]?000"
    r"|45[_,]?000|monotonic\(\)", re.I)


def grade(answer: str, order: list[str]) -> dict:
    text = answer or ""
    low = text.lower()
    names_fn = "should_retry" in text
    names_file = "backoff" in low
    units = (("millisecond" in low or re.search(r"\bms\b", low) is not None)
             and re.search(r"(?<!milli)seconds?\b", low) is not None)
    conversion = _CONVERSION.search(text) is not None
    modules = sum(1 for p in order if Path(p).stem in text)
    return {"fixed": bool(names_fn and names_file and units and conversion),
            "names_fn": names_fn, "names_file": names_file, "units": units,
            "conversion": conversion,
            "module_coverage": round(modules / len(order), 3)}


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
    cfg = LoopConfig(max_iterations=max_iterations,
                     force_answer_after=max_iterations - 2,
                     tools_available=True, think=think,
                     force_first_tool_call=False, prepare_payload=budgeter,
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

    executor = make_executor((str(project),))
    t0 = time.monotonic()
    error = None
    answer = ""
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
                           "invalid_citations", "digest")} for f in folds],
        "researcher_calls": len(calls), "tool_calls": len(rec.tools),
        "reads": len(reads), "distinct_reads": len(distinct),
        "re_reads": len(reads) - len(distinct),
        "read_order": reads,
        "compaction_calls": len(comp_calls),
        "compaction_seconds": round(sum(c["ms"] for c in comp_calls) / 1000, 1),
        "max_prompt_tokens": max((c["pt"] or 0 for c in calls), default=0),
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
             "| arm | n | fixed | compactions (mean / min–max) | "
             "fold calls | fold s / trial | files read | re-reads | "
             "coverage | errors |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        rows = [t for t in trials if t["arm"] == arm]
        cs = [r["compactions"] for r in rows]
        lines.append(
            f"| {arm} | {len(rows)} | {_pct(_rate(rows))} | "
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
    lines += ["", "## Grader", "",
              "A trial is fixed when the answer names `should_retry`, "
              "`backoff`, the seconds/milliseconds mismatch and a correct "
              "conversion. Partial credit per trial is in `trials.jsonl`.", ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://192.168.178.2:11433")
    p.add_argument("--model", default="gemma4:e4b")
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
                       / "compaction")
    out.mkdir(parents=True, exist_ok=True)
    trials_path = out / "trials.jsonl"
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    meta = {"date": date, "model": args.model, "base": args.base,
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
