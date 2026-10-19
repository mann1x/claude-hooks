"""Milestone C exit bench: fix-rate by compaction count (plan §3, Exit C).

A capable model (qwen3.6 27B on eleven2go by default: a weak model's
failure says nothing about compaction) runs the researcher's production
tool loop — the real ``_loop_payload_budgeter``, ``compaction.compact``
and the builtin read-only tools — over a generated project sized to fold
its history at least ``--folds`` times.

The window is the model's own (``--window auto``, the default): resolved
exactly as production resolves it, so the trigger, target and output cap
are the ones a council gets. ``--window 32768`` pins it on both sides of
the wire — a stress setting, at 1/8 of the budgets a 256k model gets, and
never the default again (the 32k pin ran compaction at production's
fixed-size parts with an eighth of its room). The project grows to fit by
adding modules of the 32k task's size (``scale_for()``), never by making
modules bigger: a model reads ~6 modules per turn as parallel calls, and
at 9k tokens a module one turn added ~55k and jumped the prompt past a
131k window before any check could fire (smoke, 2026-10-19).

Task v5 (2026-10-19): nothing in the harness fights the model. v4 forced
a reading order and refused every read ahead of it, locked
``limits.py`` after one read, sent an early answer back up to four times
and filled the project with word salad whose "purpose" the answer had to
give; gemma4:31b-cloud, which reads what the symptom points at, spent its
lanes on refusals and strike warnings (and on a duplicate stub that kept
each refused read refused, fixed in the loop). Now:

- modules are read in any order; the answer's **module table** needs what
  each module does, and only its code says (one operation per module, in
  its docstring and function names: :func:`_module_text`);
- every file can be read again; whether the decisecond fact survived a fold
  is read from each digest (``unit_carried``), re-reads are counted;
- the table is graded per module and split by **read phase** — how many
  folds had run when the module was first read (:class:`ReadLog`) — so a
  flow's recall of what it read before a fold is measured directly.

Task v4: the extra modules that size the project carry real-looking names
and are interleaved (``modules_for``).

Task v2 (2026-10-14). Every fact the fix needs can be learned once only,
so it has to survive the folds — v1 let the model re-read the file it had
lost, and ``basic`` (no model call) scored as well as the agentic flows:

- ``src/config/limits.py`` (read first) gives the retry window in
  **deciseconds** (``RETRY_WINDOW = 450``); nothing else in the project
  says so, and "ms vs s" intuition gives the wrong fix (×1000, not ×100).
- (v2–v4 let that file be read once; v5 lets it be re-read and reads the
  fact's survival from the digests instead.)
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

#: The stress window (``--window 32768``); production is ``auto``.
STRESS_WINDOW = 32_768
#: Record kinds per module (the main one + helpers, :func:`_module_text`):
#: ~1.7k tokens, the v4 module size, kept at every window so one turn's
#: parallel reads stay a small share of it; the project grows by modules.
FUNCS = 4
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

Audit the project under `src/` ({n} modules; `list_files` shows them).
Read every module in full with `read_file`, in any order: the module table
below needs what each module does, and only its code shows that.

Then answer with:
1. **Root cause** — the file and the function that is wrong, and why.
2. **Fix** — the exact corrected line of code.
3. **Module table** — every module under `src/`, one line each: its path
   and what it does.
"""

#: Record kinds a module works on. No time words ("window", "retry"): a
#: filler name that echoes the bug is a decoy the task never meant.
_NOUNS = ("lease segment page cursor batch shard token offset epoch frame "
          "slot entry bucket marker digest handle span mirror record "
          "ledger").split()
#: The one operation each module does: (verb, third person). Graded by the
#: verb's stem, so no stem is part of a module or package name or of a
#: word the table would use anyway (no merge/merger, encode/encoder, lock/
#: clock, trace/tracker, audit/, snapshot, paginate/pagination).
_VERBS = [
    ("reconcile", "Reconciles"), ("evict", "Evicts"), ("throttle", "Throttles"),
    ("replay", "Replays"), ("validate", "Validates"), ("compress", "Compresses"),
    ("rotate", "Rotates"), ("prune", "Prunes"), ("aggregate", "Aggregates"),
    ("sample", "Samples"), ("migrate", "Migrates"), ("checksum", "Checksums"),
    ("quarantine", "Quarantines"), ("rebalance", "Rebalances"),
    ("expire", "Expires"), ("deduplicate", "Deduplicates"),
    ("serialize", "Serializes"), ("flush", "Flushes"), ("archive", "Archives"),
    ("replicate", "Replicates"), ("coalesce", "Coalesces"),
    ("truncate", "Truncates"), ("vacuum", "Vacuums"),
]


def verb_stem(verb: str) -> str:
    """What a table row must contain: ``reconcil`` matches reconciles /
    reconciling / reconciliation."""
    return verb[:-1] if verb.endswith("e") else verb


def _main_block(rng: random.Random, verb: str, third: str, noun: str) -> str:
    n = noun
    return f'''

def {verb}_{n}s(store, {n}s, *, limit=None):
    """{third} each {n} in ``{n}s`` against what ``store`` holds."""
    done = []
    for {n} in {n}s:
        if limit is not None and len(done) >= limit:
            break
        current = store.get(_{n}_key({n}))
        if current is None:
            continue
        done.append(_{verb}_one(current, {n}))
    return done


def _{verb}_one(current, {n}):
    """{third} one {n}: the stored copy updated by the incoming one."""
    out = dict(current)
    out.update({{k: v for k, v in {n}.items() if v is not None}})
    out["version"] = current.get("version", 0) + 1
    return out


def {verb}_in_batches(store, {n}s, size={rng.choice((16, 32, 64, 128))}):
    """{third} ``{n}s`` ``size`` at a time, so one call never holds the
    store for the whole list."""
    out = []
    for i in range(0, len({n}s), size):
        out.extend({verb}_{n}s(store, {n}s[i:i + size]))
    return out
'''


def _helper_block(rng: random.Random, noun: str, stem: str) -> str:
    n = noun
    cls = n.capitalize() + stem.capitalize()
    return f'''

def _{n}_key({n}):
    return ({n}.get("shard", {rng.randint(0, 15)}), {n}.get("id"))


def {n}_stats({n}s):
    """Count and size of ``{n}s``."""
    sizes = [len(x.get("payload") or b"") for x in {n}s]
    if not sizes:
        return {{"count": 0, "bytes": 0, "largest": 0}}
    return {{"count": len(sizes), "bytes": sum(sizes), "largest": max(sizes)}}


def unclaimed_{n}s({n}s, owners):
    """The {n}s no live owner claims."""
    return [x for x in {n}s if x.get("owner") not in owners]


class {cls}:
    """The {n}s this {stem} works on, at most ``capacity`` at a time."""

    def __init__(self, capacity={rng.choice((256, 512, 1024, 4096))}):
        self.capacity = capacity
        self._items = {{}}

    def add(self, {n}):
        if len(self._items) >= self.capacity:
            raise OverflowError("{stem} full: %d {n}s" % self.capacity)
        self._items[_{n}_key({n})] = {n}

    def pending(self):
        return [v for v in self._items.values() if not v.get("done")]
'''


def _module_text(rng: random.Random, module: str, n_kinds: int = FUNCS,
                 verb: Optional[tuple[str, str]] = None) -> str:
    """A module's body: with ``verb``, one main operation on its first
    record kind (docstring + functions named for it), then helpers for
    ``n_kinds`` kinds. Code that does something, so a "what it does" line
    can be right or wrong."""
    stem = module.split("/")[-1].split("_")[-1]
    first = module.split("/")[-1].split("_")[0]
    nouns = [first if first in _NOUNS else rng.choice(_NOUNS)]
    while len(nouns) < max(1, n_kinds):
        nouns.append(rng.choice([x for x in _NOUNS if x not in nouns]))
    out = []
    if verb is not None:
        out.append(_main_block(rng, verb[0], verb[1], nouns[0]))
    for noun in nouns:
        out.append(_helper_block(rng, noun, stem))
    return "".join(out)


_PACKAGES = sorted({m.split("/")[0] for m in MODULES} | {
    "replica", "audit", "admin", "cluster", "ingest", "cli", "plugins",
    "storage", "schedule", "telemetry"})
_STEMS = ("router manager registry handler adapter loader writer reader "
          "planner tracker resolver encoder decoder monitor broker gateway "
          "builder sweeper keeper merger splitter watcher scanner policy "
          "session catalog snapshot rollup quota limiter").split()


def modules_for(extra: int, seed: int = 11) -> list[str]:
    """:data:`MODULES` plus ``extra`` modules named like the real ones and
    spread among them (``config/limits`` stays first, ``net/backoff``
    last). Named ``ext/part_NNN`` and grouped, they read as filler: the
    model skipped all 127 and read 28 of 155 (smoke, 2026-10-19)."""
    rng = random.Random(seed)
    names: set[str] = set(MODULES)
    bases: set[str] = {m.split("/")[-1] for m in MODULES}
    fill = []
    while len(fill) < extra:
        a, b = rng.choice(_NOUNS), rng.choice(_STEMS)
        name = f"{rng.choice(_PACKAGES)}/{a}_{b}"
        # Basenames are unique: the table grader finds a module's row by it.
        if name not in names and name.split("/")[-1] not in bases:
            names.add(name)
            bases.add(name.split("/")[-1])
            fill.append(name)
    middle = MODULES[1:-1] + fill
    rng.shuffle(middle)
    return MODULES[:1] + middle + MODULES[-1:]


def build_project(dest: Path, *, seed: int = 7, funcs: int = FUNCS,
                  extra: int = 0) -> tuple[list[str], dict[str, str]]:
    """Write the project; returns the module paths (under src/) and each
    graded module's operation verb. ``config/limits`` and ``net/backoff``
    carry the bug and are not graded in the table."""
    rng = random.Random(seed)
    order, purposes = [], {}
    for mod in modules_for(extra):
        path = dest / "src" / f"{mod}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        rel = f"src/{mod}.py"
        if mod == "config/limits":
            text = LIMITS_HEAD + _module_text(rng, mod, funcs)
        elif mod == "net/backoff":
            text = ('"""ledgerd net.backoff: when to retry a failed request '
                    'and how long to wait."""\n\nimport random\nimport time'
                    '\n\nfrom ledgerd.config import limits\n\nMAX_ATTEMPTS = 6\n'
                    + _module_text(rng, mod, funcs) + BACKOFF_TAIL)
        else:
            verb = rng.choice(_VERBS)
            kind = mod.split("/")[-1].split("_")[0]
            kind = kind if kind in _NOUNS else "record"
            what = "records" if kind == "record" else f"{kind} records"
            head = (f'"""ledgerd {mod.replace("/", ".")}: {verb[1]} {what} '
                    f'for the {mod.split("/")[0]} layer."""\n')
            text = head + _module_text(rng, mod, funcs, verb=verb)
            purposes[rel] = verb[0]
        path.write_text(text, encoding="utf-8")
        order.append(rel)
    return order, purposes


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #

TASK_VERSION = 5
TICKET = "LDG-4471"
FOLLOWUP_AFTER = 8
FOLLOWUP = (f"One more thing from the on-call lead: whatever you find, the "
            f"answer must cite ticket {TICKET}, which tracks this incident.")

#: A ×100 conversion between deciseconds and milliseconds, either side.
_CONVERSION = re.compile(
    r"RETRY_WINDOW\s*\*\s*100\b|100\s*\*\s*(limits\.)?RETRY_WINDOW"
    r"|elapsed\s*(//|/)\s*100\b|//\s*100[_,]?000[_,]?000\b"
    r"|//\s*1e8|45[_,]?000\b", re.I)


def grade_table(answer: str, purposes: dict[str, str],
                phases: dict[str, int]) -> dict:
    """Per graded module: does a line naming it (by basename) also name its
    operation? Split by read phase (folds run before its first read;
    ``unread`` = never read). Returns ``{phase: [right, total]}``."""
    lines = (answer or "").lower().splitlines()
    by: dict[str, list[int]] = {}
    for path, verb in purposes.items():
        base = Path(path).stem.lower()
        pkg = Path(path).parent.name.lower()
        # A row names the module as `<base>.py` or `<pkg>/<base>`; a bare
        # word ("queue") may sit in another module's row.
        names = re.compile(rf"\b{re.escape(base)}\.py\b|"
                           rf"\b{re.escape(pkg)}/{re.escape(base)}\b")
        stem = verb_stem(verb)
        right = any(stem in names.sub("", ln.replace(path.lower(), ""))
                    for ln in lines if names.search(ln))
        ph = phases.get(path)
        key = "unread" if ph is None else str(ph)
        cell = by.setdefault(key, [0, 0])
        cell[0] += int(right)
        cell[1] += 1
    return dict(sorted(by.items()))


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


def _norm(args) -> str:
    try:
        v = (json.loads(args) if isinstance(args, str)
             else dict(args or {})).get("path") or ""
    except (TypeError, ValueError, AttributeError):
        v = ""
    v = str(v).replace("\\", "/")
    i = v.find("src/")
    return v[i:] if i >= 0 else v.lstrip("./")


class ReadLog:
    """The builtin executor, unchanged, noting each module's **read phase**:
    how many folds had run when it was first read (task v5). Nothing is
    refused, hidden or locked."""

    def __init__(self, inner, rec: "Recorder",
                 limits: str = "src/config/limits.py"):
        self.inner = inner
        self.rec = rec
        self.limits = limits
        self.phase: dict[str, int] = {}
        self.limits_reads = 0
        self.limits_rereads_after_fold = 0

    def _folds(self) -> int:
        return sum(1 for e in self.rec.events if e.get("kind") == "compaction")

    def __call__(self, name, args, cwd):
        out = self.inner(name, args, cwd)
        if name == "read_file" and not str(out).lower().startswith("error"):
            path = _norm(args)
            folds = self._folds()
            self.phase.setdefault(path, folds)
            if path == self.limits:
                self.limits_reads += 1
                if self.limits_reads > 1 and folds > 0:
                    self.limits_rereads_after_fold += 1
        return out


def with_followup(prepare, *, after: int = FOLLOWUP_AFTER):
    """``prepare`` plus one typed user follow-up once ``after`` tool
    results are in the history — a typed prompt, not a harness note, so
    the cut planner treats it as the user's."""
    sent = {"done": False}
    #: generation -> whether that generation's marker carries the unit,
    #: read from the history each call is actually sent with.
    carried: dict[int, bool] = {}
    #: generation -> that marker's text, as sent (why a fact was lost).
    markers: dict[int, str] = {}

    def wrapped(payload):
        msgs = payload.get("messages") or []
        if not sent["done"] and sum(1 for m in msgs
                                    if m.get("role") == "tool") >= after:
            msgs.append({"role": "user", "content": FOLLOWUP})
            payload["messages"] = msgs
            sent["done"] = True
        out = prepare(payload)
        sent_msgs = ((out if isinstance(out, dict) else payload).get("messages")
                     or [])
        wrapped.last = list(sent_msgs)
        for m in sent_msgs:
            if m.get("_compaction_marker"):
                raw = str(m.get("content") or "")
                text = raw.lower()
                gen = int(m.get("_compaction_generation") or 1)
                markers[gen] = raw
                carried[gen] = (
                    "decisecond" in text or "tenths of a second" in text)
        return out

    for attr in ("observe", "recover", "session"):
        if hasattr(prepare, attr):
            setattr(wrapped, attr, getattr(prepare, attr))
    wrapped.sent = sent
    wrapped.carried = carried
    wrapped.markers = markers
    wrapped.last = []
    return wrapped


# --------------------------------------------------------------------------- #
# One trial
# --------------------------------------------------------------------------- #

def scale_for(*, trigger: int, target: int, folds: int,
              margin: float = 1.25) -> tuple[int, int, int]:
    """``(funcs, extra_modules, read_tokens)`` for a project whose reads
    reach the trigger ``folds`` times: the first fill to the trigger, then
    ``trigger - target`` per later fold, times ``margin``. Modules keep
    :data:`FUNCS`; the count grows."""
    from claude_hooks import token_calib
    need = int(margin * (trigger + (folds - 1) * max(1, trigger - target)))
    with __import__("tempfile").TemporaryDirectory() as d:
        order, _ = build_project(Path(d), funcs=FUNCS)
        per_module = token_calib.estimate_tokens(
            sum(len((Path(d) / p).read_text()) for p in order), "") / len(order)
    extra = max(0, int(need / per_module) + 1 - len(MODULES))
    return FUNCS, extra, need


def resolved_budget(client, model: str, knobs) -> "budget.Budget":
    """The budget production gives this model's researcher, sized on the
    loop's own fixed prompt (system + question + tool schemas)."""
    msgs = [{"role": "system", "content": council.RESEARCHER_SYSTEM},
            {"role": "user", "content": QUESTION.format(n=0)}]
    return budget.plan(client, model, msgs, tools=openai_tool_specs(),
                       knobs=knobs)


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


def make_client(base: str, window: Optional[int]):
    """The production client (``window`` None), or the pinned stress one."""
    if window:
        return PinnedClient(base, window)
    return ChatClient(base, timeout_s=900.0)


def run_trial(*, base: str, model: str, arm: str, idx: int, project: Path,
              order: list[str], max_iterations: int, think,
              window: Optional[int] = None,
              purposes: Optional[dict[str, str]] = None) -> dict:
    flow, placement = arm.split("/")
    client = make_client(base, window)
    rec = Recorder()
    # A pinned window is pinned in the budget too, not only on the wire: a
    # cloud model's own window outranks the client's, and gemma4:31b-cloud
    # ran to 56k prompt tokens on a "32k" window with no compaction at all.
    knobs = budget.Knobs(num_ctx=window,
                         compaction=compaction.Settings(flow=flow,
                                                        placement=placement))
    budgeter = council._loop_payload_budgeter(
        client, model, role="researcher", knobs=knobs, recorder=rec,
        round=0, lane_idx=idx)
    prepare = with_followup(budgeter)
    executor = ReadLog(make_executor((str(project),)), rec)
    cfg = LoopConfig(max_iterations=max_iterations,
                     force_answer_after=max_iterations - 2,
                     tools_available=True, think=think,
                     force_first_tool_call=False, prepare_payload=prepare,
                     on_response=budgeter.observe)
    question = QUESTION.format(n=len(order))
    payload = {"model": model, "stream": False, "think": think,
               "messages": [{"role": "system", "content":
                             council.RESEARCHER_SYSTEM},
                            {"role": "user", "content": question}]}

    def on_iter(_i, req, resp, ms):
        pt, ct = council._usage_from(resp)
        rec.record_llm(role="researcher", round=0, lane_idx=idx, model=model,
                       request=req, response=resp, prompt_tokens=pt,
                       completion_tokens=ct, duration_ms=ms)

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
        "arm": arm, "trial": idx, "model": model,
        "window": window or resolved_budget(client, model, knobs).context_length,
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
        "marker_texts": [prepare.markers[g] for g in sorted(prepare.markers)],
        "limits_reads": executor.limits_reads,
        "limits_rereads_after_fold": executor.limits_rereads_after_fold,
        "modules_read": sum(1 for p in order if p in executor.phase),
        "read_phase": executor.phase,
        #: {phase: [right, total]} of the module table (task v5).
        "table": grade_table(answer, purposes or {}, executor.phase),
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
             f"Model `{meta['model']}` at `{meta['base']}`, "
             f"{meta['window_note']}, think={meta['think']}, "
             f"{meta['trials']} trials per arm, commit `{meta['commit']}`.",
             "", f"Project: {meta['modules']} modules × {meta['funcs']} "
             f"functions, ~{meta['read_tokens']:,} tokens of reads, sized "
             f"for {meta['folds']} folds (trigger {meta['trigger']:,}, "
             f"target {meta['target']:,}, output cap {meta['cap']:,}).",
             "", "## By arm", "",
             "| arm | n | answered | fixed (of answered) | "
             "compactions (mean / min–max) | "
             "fold calls | fold s / trial | modules read | re-reads | "
             "errors |",
             "|---|---|---|---|---|---|---|---|---|---|"]
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
            f"{sum(r.get('modules_read', 0) for r in rows) / len(rows):.1f}"
            f"/{meta['modules']} | "
            f"{sum(r['re_reads'] for r in rows) / len(rows):.1f} | "
            f"{sum(1 for r in rows if r['error'])} |")
    lines += ["", "## Module table by read phase", "",
              "Share of graded modules whose table row names what the module "
              "does, by how many folds had run when it was first read "
              "(0 = before the first fold). What a flow keeps across a fold "
              "shows as the gap between phase 0 and the last phase.", ""]
    phases = sorted({k for t in trials for k in (t.get("table") or {})},
                    key=lambda k: (k == "unread", k))
    lines += ["| arm | " + " | ".join(
        "unread" if k == "unread" else f"phase {k}" for k in phases) + " |",
        "|---|" + "---|" * len(phases)]
    for arm in arms:
        rows = [t for t in trials if t["arm"] == arm]
        cells = []
        for k in phases:
            right = sum((r.get("table") or {}).get(k, [0, 0])[0] for r in rows)
            total = sum((r.get("table") or {}).get(k, [0, 0])[1] for r in rows)
            cells.append(f"{_pct(right / total) if total else '—'} ({total})")
        lines.append(f"| {arm} | " + " | ".join(cells) + " |")
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
              "limits.py re-read after a fold |", "|---|---|---|---|---|"]
    for arm in arms:
        rows = [t for t in trials if t["arm"] == arm]
        lines.append(
            f"| {arm} | {sum(r.get('units', False) for r in rows)}/{len(rows)} | "
            f"{sum(r.get('conversion', False) for r in rows)}/{len(rows)} | "
            f"{sum(r.get('ticket', False) for r in rows)}/{len(rows)} | "
            f"{sum(r.get('limits_rereads_after_fold', 0) for r in rows)} |")
    lines += ["", "## Grader", "",
              f"Task v{TASK_VERSION}. A trial is fixed when the answer names "
              "`should_retry` and `backoff`, the decisecond unit, a ×100 "
              f"conversion and ticket `{TICKET}`. Nothing is refused or "
              "locked; the unit's survival per fold is read from the digests "
              "(`unit_carried` in trials.jsonl).", ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://192.168.178.161:22434")
    p.add_argument("--model", default="qwen36-base-mtp_tb:27b-q4km-128k")
    p.add_argument("--arms", default="reduced/system,reduced/user,"
                                     "full/system,full/user,basic/system")
    p.add_argument("--trials", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--max-iterations", type=int, default=0,
                   help="0 = modules + 20.")
    p.add_argument("--window", default="auto",
                   help="auto (the model's own, as production resolves it) "
                        "or a token count to pin (stress: 32768).")
    p.add_argument("--folds", type=int, default=3,
                   help="Size the project to fold at least this often.")
    p.add_argument("--funcs", type=int, default=0,
                   help="Override the functions per module (0 = scale).")
    p.add_argument("--extra-modules", type=int, default=-1,
                   help="Override the filler modules (-1 = scale).")
    p.add_argument("--plan-only", action="store_true",
                   help="Print the resolved window and project size; run "
                        "nothing.")
    # Production researchers run think="high" (927 of 927 gemma calls in
    # the council transcripts); bare --think keeps the old True.
    p.add_argument("--think", nargs="?", const=True, default=False,
                   choices=[True, "low", "medium", "high", "max"],
                   type=lambda v: v if v != "true" else True)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args(argv)

    window = None if args.window == "auto" else int(args.window)
    probe = make_client(args.base, window)
    plan = resolved_budget(probe, args.model, budget.Knobs(num_ctx=window))
    target = budget.compaction_target_tokens(
        plan.context_length, overhead_tokens=plan.prompt_tokens,
        trigger_tokens=plan.trigger_tokens)
    funcs, extra, need = scale_for(trigger=plan.trigger_tokens or 0,
                                   target=target, folds=args.folds)
    if args.funcs:
        funcs = args.funcs
    if args.extra_modules >= 0:
        extra = args.extra_modules
    n_modules = len(MODULES) + extra
    max_iterations = args.max_iterations or n_modules + 20
    window_note = (f"window pinned to {window:,} tokens (stress)" if window
                   else f"window {plan.context_length:,} "
                        f"({plan.window_source}, as production resolves it)")
    print(f"{args.model}: {window_note}; trigger {plan.trigger_tokens:,}, "
          f"target {target:,}, cap {plan.output_tokens:,}; project "
          f"{n_modules} modules x {funcs} funcs (~{need:,} tok of reads), "
          f"max_iterations {max_iterations}", flush=True)
    if args.plan_only:
        return 0
    date = dt.date.today().isoformat()
    out = args.out or (ROOT / "benchmarks/consultants/results" / date
                       / f"compaction-v{TASK_VERSION}")
    out.mkdir(parents=True, exist_ok=True)
    trials_path = out / "trials.jsonl"
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    meta = {"date": date, "task_version": TASK_VERSION,
            "model": args.model, "base": args.base,
            "window": window or plan.context_length,
            "window_source": "pinned" if window else plan.window_source,
            "window_note": window_note, "trigger": plan.trigger_tokens,
            "target": target, "cap": plan.output_tokens, "folds": args.folds,
            "funcs": funcs, "modules": n_modules, "read_tokens": need,
            "think": args.think, "trials": args.trials,
            "arms": args.arms.split(","), "commit": commit,
            "max_iterations": max_iterations,
            "fork_baseline": FORK_BASELINE}

    if not args.report_only:
        project = out / "project"
        order, purposes = build_project(project, funcs=funcs, extra=extra)
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
                                max_iterations=max_iterations,
                                think=args.think, window=window,
                                purposes=purposes): (arm, i)
                    for arm, i in jobs}
            for fut in cf.as_completed(futs):
                t = fut.result()
                with lock, trials_path.open("a") as f:
                    f.write(json.dumps(t) + "\n")
                tab = t.get("table") or {}
                right = sum(v[0] for v in tab.values())
                total = sum(v[1] for v in tab.values())
                print(f"{t['arm']:15s} #{t['trial']} fixed={t['fixed']} "
                      f"folds={t['compactions']} calls={t['researcher_calls']}"
                      f" read={t.get('modules_read')} table={right}/{total}"
                      f" {t['seconds']}s err={t['error']}", flush=True)
    trials = [json.loads(x) for x in trials_path.read_text().splitlines()]
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    (out / "report.md").write_text(render_report(trials, meta))
    print(out / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
