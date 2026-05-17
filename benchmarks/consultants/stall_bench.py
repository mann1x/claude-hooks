"""M11a stall skill-eval bench — Tier 1 (standalone) + Tier 2 (council).

Measures per-model token-streaming cadence under two workload
shapes, derives recommended ``(stall_threshold_s, hard_cap_s)`` per
model, and writes a results bundle (``metadata.json`` +
``trials.jsonl`` + ``report.md``) under
``benchmarks/consultants/results/<YYYY-MM-DD>/stall/``.

The harness pairs with:

- :mod:`benchmarks.consultants.harness` for the question loader,
  ``StallTrial`` schema, ``SuiteManifest``, and cost estimator.
- :mod:`benchmarks.consultants.stall_capture` for the
  ``TimingCaptureChat`` wrapper + ``CallTiming`` recording.
- :mod:`consultants.engine.stall_defaults` for the per-model
  defaults the live-run commit (M11a-2) will populate.

**M11a-1 (this file's first landing)** ships the harness with full
dry-run support and live-run code paths. M11a-2 fires the live
run against the live Ollama-Pro proxy with explicit
``--accept-cost``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from benchmarks.consultants.harness import (
    HARNESS_VERSION,
    BenchQuestion,
    StallTrial,
    SuiteManifest,
    append_trial,
    estimate_stall_cost,
    load_questions,
    load_suite_manifest,
)
from benchmarks.consultants.stall_capture import (
    CallTiming,
    TimingCaptureChat,
    aggregate_calls,
)
from consultants.engine.stall_defaults import (
    StallThresholds,
    RECOMMENDED_DEFAULT_STALL,
)

log = logging.getLogger("benchmarks.consultants.stall_bench")


# ---------------------------------------------------------------------- #
# Constants
# ---------------------------------------------------------------------- #

DEFAULT_OLLAMA_BASE = "http://192.168.178.2:11433"

# Default model cohort — same 6 as M11b-mlang baseline + the
# gemini-3-flash-preview that triggered the 2026-05-15 audit
# pathology.
DEFAULT_MODELS: tuple[str, ...] = (
    "glm-5.1:cloud",
    "kimi-k2.6:cloud",
    "gemma4:31b-cloud",
    "qwen3-coder-next:cloud",
    "deepseek-v4-pro:cloud",
    "deepseek-v4-flash:cloud",
    "gemini-3-flash-preview:cloud",
)

# Default trial counts (overridable via --trials).
DEFAULT_TIER1_TRIALS = 3
DEFAULT_TIER2_TRIALS = 2

# Effort used by Tier 2 council runs. Medium avoids x-tier
# multi-model fanout (which would conflate the model under test
# with the fanout cohort).
TIER2_COUNCIL_EFFORT = "medium"


# ---------------------------------------------------------------------- #
# Dry-run client
# ---------------------------------------------------------------------- #

class _DryRunStreamingClient:
    """Stub chat client that emits a deterministic burst of tokens
    via ``on_token`` callbacks, then returns an OpenAI-shape dict.

    Used by ``--dry-run`` for both tiers. Token counts + inter-token
    timing are configurable; defaults produce a moderate-length
    response so the captured ``CallTiming`` has enough samples for
    the percentile helpers to return non-None values.
    """

    def __init__(self, *,
                 tokens: int = 30,
                 ttft_ms: float = 80.0,
                 gap_ms: float = 25.0,
                 jitter_token: int = 0,
                 jitter_ms: float = 0.0):
        self.tokens = tokens
        self.ttft_ms = ttft_ms
        self.gap_ms = gap_ms
        self.jitter_token = jitter_token   # which token gets jittered
        self.jitter_ms = jitter_ms          # extra gap on that token
        self.last_inference_s = 0.0
        self.total_inference_s = 0.0

    def chat(self, payload: dict, *, think: bool = False) -> dict:
        return {
            "choices": [{
                "message": {"role": "assistant",
                            "content": "stub response"},
            }],
            "usage": {"prompt_tokens": 1500,
                      "completion_tokens": self.tokens,
                      "total_tokens": 1500 + self.tokens},
        }

    def chat_streamed(self, payload: dict, *,
                      on_token: Optional[Callable[[str], None]] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      ) -> dict:
        t0 = time.monotonic()
        # TTFT
        time.sleep(self.ttft_ms / 1000.0)
        for i in range(self.tokens):
            if on_token is not None:
                on_token(f"t{i}")
            if cancel_check is not None and cancel_check():
                from consultants.engine.stall import CancelledByOrchestrator
                raise CancelledByOrchestrator("stub-cancel")
            gap = self.gap_ms
            if i == self.jitter_token and self.jitter_ms > 0:
                gap += self.jitter_ms
            time.sleep(gap / 1000.0)
        elapsed = time.monotonic() - t0
        self.last_inference_s = elapsed
        self.total_inference_s += elapsed
        return {
            "choices": [{
                "message": {"role": "assistant",
                            "content": " ".join(f"t{i}" for i in range(self.tokens))},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1500,
                      "completion_tokens": self.tokens,
                      "total_tokens": 1500 + self.tokens},
        }

    def reset_inference_timer(self) -> None:
        self.last_inference_s = 0.0
        self.total_inference_s = 0.0


# ---------------------------------------------------------------------- #
# Live client factory
# ---------------------------------------------------------------------- #

def _make_live_chat_client(model: str, ollama_base: str) -> Any:
    """Build a real ChatClient for the given model + proxy URL.

    Lazy-imports so the dry-run path doesn't pay the get_advice
    import cost (and so the test env without claude_hooks
    installed can still import this module).
    """
    from claude_hooks.get_advice.chat_client import make_agent_chat_client

    return make_agent_chat_client(model, ollama_base)


# ---------------------------------------------------------------------- #
# Tier 1 — standalone
# ---------------------------------------------------------------------- #

def run_standalone_trial(question: BenchQuestion,
                         model: str,
                         trial_idx: int,
                         *,
                         capture: TimingCaptureChat,
                         suite_hash: str = "",
                         ) -> StallTrial:
    """One Tier-1 trial: a single ``chat_streamed`` call.

    The wrapped client records exactly one ``CallTiming``; the
    trial reports that call's percentiles directly (no aggregation
    needed for a single call).
    """
    timestamp = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    capture.clear_calls()
    capture.reset_inference_timer()

    payload = {
        "model": model,
        "messages": [
            {"role": "user",
             "content": _build_user_message(question)},
        ],
        "stream": True,
    }

    t0 = time.monotonic()
    error: Optional[str] = None
    try:
        capture.chat_streamed(payload)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    wall_s = time.monotonic() - t0

    agg = aggregate_calls(capture.calls)
    trial = StallTrial(
        question_id=question.id,
        tier="standalone",
        model=model,
        trial_idx=trial_idx,
        timestamp=timestamp,
        suite_hash=suite_hash,
        error=error,
        wall_s=wall_s,
        inference_s=float(capture.total_inference_s),
        num_chat_calls=agg.num_calls,
        total_tokens=agg.total_tokens,
        time_to_first_token_p50_ms=float(agg.time_to_first_token_p50_ms or 0.0),
        time_to_first_token_p99_ms=float(agg.time_to_first_token_p99_ms or 0.0),
        inter_token_p50_ms=float(agg.inter_token_p50_ms or 0.0),
        inter_token_p99_ms=float(agg.inter_token_p99_ms or 0.0),
        call_timings=[c.to_dict() for c in capture.calls],
        council_effort="",
        council_node_set=[],
        stall_events_count=0,
        stall_cancelled_count=agg.cancelled_count,
    )
    return trial


def _build_user_message(question: BenchQuestion) -> str:
    """Compose the user-side prompt from the question's task +
    body. Used by both tiers; the council node uses the same
    text as the user question.
    """
    parts = [question.task.strip()]
    if question.body:
        parts.append("")
        parts.append(question.body.strip())
    return "\n".join(parts)


# ---------------------------------------------------------------------- #
# Tier 2 — fake-consultancy
# ---------------------------------------------------------------------- #

def run_council_trial(question: BenchQuestion,
                      model: str,
                      trial_idx: int,
                      *,
                      capture: TimingCaptureChat,
                      suite_hash: str = "",
                      cwd: str = "/tmp",
                      effort: str = TIER2_COUNCIL_EFFORT,
                      ) -> StallTrial:
    """One Tier-2 trial: full ``build_council_graph`` run with all
    roles pinned to the same model under test.

    The ``capture`` argument is shared across all roles in the
    council — that's deliberate. We want one aggregated
    percentile per model per trial; sharing the wrapper lets
    ``capture.calls`` collect every chat call the council fired.

    Lazy-imports langgraph so the dry-run path can still import
    this module in environments without langgraph installed.
    """
    timestamp = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    capture.clear_calls()
    capture.reset_inference_timer()

    # Lazy imports — keep the module importable in main env.
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from consultants.engine.graph import GraphDeps, build_council_graph
    except ImportError as e:
        error = f"ImportError: {e} (need langgraph + consultants env)"
        log.error("council trial unavailable: %s", error)
        return StallTrial(
            question_id=question.id,
            tier="council",
            model=model,
            trial_idx=trial_idx,
            timestamp=timestamp,
            suite_hash=suite_hash,
            error=error,
            council_effort=effort,
        )

    # All roles share the same wrapper so we capture every call.
    enabled_roles = ("planner", "researcher", "synthesizer")
    deps = GraphDeps(
        chat_clients={r: capture for r in enabled_roles},
        models={r: model for r in enabled_roles},
        enabled_roles=enabled_roles,
        cwd=cwd,
        tool_executor=lambda *a, **kw: "",
        tool_specs=[],
        grounding_msgs=[],
        disable_cache=True,
    )

    graph = build_council_graph(deps, checkpointer=InMemorySaver())
    thread_config = {
        "configurable": {
            "thread_id": f"stall-bench-{question.id}-{model}-{trial_idx}",
        },
    }
    # ``runtime_control`` MUST be set for the researcher node to
    # route chat through ``stall_protected_chat_fn_for``, which is
    # the only code path that exercises ``chat_streamed`` (and
    # therefore the only one our ``TimingCaptureChat`` can record
    # per-token timing from). Without this the council falls back
    # to ``chat_client.chat`` unwrapped — we'd get coarse TTFT
    # only, no inter-token data.
    #
    # Values mirror the production defaults from
    # ``consultants/engine/control.py`` so the bench measures the
    # workload under the same wrappers the live runtime uses.
    initial = {
        "question": _build_user_message(question),
        "cwd": cwd,
        "models": deps.models,
        "topology": "council",
        "effort": effort,
        "runtime_control": {
            "stall_threshold_s": 300.0,
            "per_lane_hard_s": 3600.0,
            "stall_retries": 1,
        },
    }

    t0 = time.monotonic()
    error: Optional[str] = None
    final_answer = ""
    node_set: list[str] = []
    try:
        final = graph.invoke(initial, config=thread_config)
        final_answer = (final.get("final_answer") or "") if isinstance(final, dict) else ""
        turns = final.get("turns") if isinstance(final, dict) else None
        if isinstance(turns, list):
            node_set = sorted({t[0] for t in turns if isinstance(t, (list, tuple)) and t})
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    wall_s = time.monotonic() - t0

    agg = aggregate_calls(capture.calls)
    trial = StallTrial(
        question_id=question.id,
        tier="council",
        model=model,
        trial_idx=trial_idx,
        timestamp=timestamp,
        suite_hash=suite_hash,
        error=error,
        wall_s=wall_s,
        inference_s=float(capture.total_inference_s),
        num_chat_calls=agg.num_calls,
        total_tokens=agg.total_tokens,
        time_to_first_token_p50_ms=float(agg.time_to_first_token_p50_ms or 0.0),
        time_to_first_token_p99_ms=float(agg.time_to_first_token_p99_ms or 0.0),
        inter_token_p50_ms=float(agg.inter_token_p50_ms or 0.0),
        inter_token_p99_ms=float(agg.inter_token_p99_ms or 0.0),
        call_timings=[c.to_dict() for c in capture.calls],
        council_effort=effort,
        council_node_set=node_set,
        council_final_answer_len=len(final_answer),
        stall_events_count=0,
        stall_cancelled_count=agg.cancelled_count,
    )
    return trial


# ---------------------------------------------------------------------- #
# Threshold derivation rule
# ---------------------------------------------------------------------- #

def derive_thresholds(trials_for_model: list[StallTrial],
                      *,
                      rubric: Optional[dict] = None,
                      ) -> StallThresholds:
    """Pick ``(stall_threshold_s, hard_cap_s)`` for one model from
    its trial batch.

    Per the M11a plan:

    ``stall_threshold_s = max(p99_inter_token_ms, p99_ttft_ms) * 2.5 / 1000``,
                          rounded up to the nearest 30 s, floored at
                          30 s, ceiled at 600 s.

    ``hard_cap_s        = p99(wall_s) * 3.0``,
                          rounded up to the nearest 60 s, floored at
                          300 s, ceiled at 3600 s.

    Tier 2 (council) trials are preferred when present — they
    capture the real workload shape under load. Tier 1 (standalone)
    trials are the fallback so models that only ran Tier 1 still get
    a defensible default.

    Multipliers + clamps come from ``rubric`` (the SUITE.md rubric
    block); when ``rubric`` is None, falls back to the documented
    defaults.

    Returns ``RECOMMENDED_DEFAULT_STALL`` when no usable trials
    exist (all errored, or the list is empty).
    """
    rubric = rubric or {}
    stall_mul = float(rubric.get("stall_margin_factor", 2.5))
    cap_mul = float(rubric.get("hardcap_margin_factor", 3.0))
    stall_min = float(rubric.get("stall_min_s", 30.0))
    stall_max = float(rubric.get("stall_max_s", 600.0))
    stall_round = float(rubric.get("stall_round_s", 30.0))
    cap_min = float(rubric.get("hardcap_min_s", 300.0))
    cap_max = float(rubric.get("hardcap_max_s", 3600.0))
    cap_round = float(rubric.get("hardcap_round_s", 60.0))

    # Prefer council trials when available.
    council_trials = [t for t in trials_for_model
                      if t.tier == "council" and t.error is None]
    standalone_trials = [t for t in trials_for_model
                         if t.tier == "standalone" and t.error is None]
    chosen = council_trials if council_trials else standalone_trials
    if not chosen:
        return RECOMMENDED_DEFAULT_STALL

    inter = [t.inter_token_p99_ms for t in chosen if t.inter_token_p99_ms > 0]
    ttft = [t.time_to_first_token_p99_ms for t in chosen
            if t.time_to_first_token_p99_ms > 0]
    walls = [t.wall_s for t in chosen if t.wall_s > 0]

    if not inter and not ttft:
        return RECOMMENDED_DEFAULT_STALL

    # Take the per-trial worst-case across the batch (the bench
    # measured p99 within each trial; we want the worst trial's
    # p99 to drive the default — anything less and we'd
    # under-protect when a model has a bad day).
    max_inter_ms = max(inter) if inter else 0.0
    max_ttft_ms = max(ttft) if ttft else 0.0
    stall_basis_ms = max(max_inter_ms, max_ttft_ms)
    stall_s = stall_basis_ms * stall_mul / 1000.0
    stall_s = _ceil_to(stall_s, stall_round)
    stall_s = max(stall_min, min(stall_max, stall_s))

    p99_wall_s = max(walls) if walls else 0.0
    cap_s = p99_wall_s * cap_mul
    cap_s = _ceil_to(cap_s, cap_round)
    cap_s = max(cap_min, min(cap_max, cap_s))

    return StallThresholds(stall_threshold_s=stall_s, hard_cap_s=cap_s)


def _ceil_to(value: float, step: float) -> float:
    """Round ``value`` up to the next multiple of ``step``."""
    if step <= 0:
        return float(value)
    import math
    return math.ceil(value / step) * step


# ---------------------------------------------------------------------- #
# Report rendering
# ---------------------------------------------------------------------- #

def render_report(*,
                  trials: list[StallTrial],
                  manifest: SuiteManifest,
                  models: list[str],
                  mode: str,
                  ollama_base: Optional[str],
                  ) -> str:
    """Render a per-model summary table from the trial batch.

    Output is plain markdown — same shape as the coder bench's
    report.md. The M11a-2 closeout commit pastes a row from this
    into ``docs/consultants-skill-eval-baselines.md``.
    """
    lines: list[str] = []
    lines.append(
        f"# Stall Skill-Eval Report — "
        f"suite v{manifest.suite_version} (hash {manifest.suite_hash[:8]})"
    )
    lines.append("")
    lines.append(f"- **Mode:** `{mode}`")
    lines.append(f"- **Ollama base:** `{ollama_base or '(n/a)'}`")
    lines.append(f"- **Trials run:** {len(trials)}")
    lines.append(f"- **Models:** {len(models)}")
    lines.append(f"- **Harness version:** {HARNESS_VERSION}")
    lines.append("")

    # Group by model.
    by_model: dict[str, list[StallTrial]] = {}
    for t in trials:
        by_model.setdefault(t.model, []).append(t)

    lines.append("## Per-model summary")
    lines.append("")
    lines.append(
        "| Model | trials | T1 | T2 | p50 TTFT (ms) | p99 TTFT (ms) | "
        "p50 inter (ms) | p99 inter (ms) | p99 wall (s) | "
        "→ stall_s | → hard_cap_s |"
    )
    lines.append(
        "|-------|-------:|---:|---:|--------------:|--------------:|"
        "---------------:|---------------:|-------------:|"
        "---------:|------------:|"
    )
    for model in models:
        ts = by_model.get(model, [])
        if not ts:
            lines.append(
                f"| `{model}` | 0 | 0 | 0 | — | — | — | — | — | — | — |"
            )
            continue
        n_t1 = sum(1 for t in ts if t.tier == "standalone")
        n_t2 = sum(1 for t in ts if t.tier == "council")
        clean = [t for t in ts if t.error is None]
        if not clean:
            lines.append(
                f"| `{model}` | {len(ts)} | {n_t1} | {n_t2} | "
                f"(all errored) | — | — | — | — | — | — |"
            )
            continue
        p50_ttft = _median([t.time_to_first_token_p50_ms
                             for t in clean if t.time_to_first_token_p50_ms > 0])
        p99_ttft = _max_or_zero([t.time_to_first_token_p99_ms for t in clean])
        p50_inter = _median([t.inter_token_p50_ms
                              for t in clean if t.inter_token_p50_ms > 0])
        p99_inter = _max_or_zero([t.inter_token_p99_ms for t in clean])
        p99_wall = _max_or_zero([t.wall_s for t in clean])

        thresholds = derive_thresholds(ts, rubric=manifest.rubric)

        lines.append(
            f"| `{model}` | {len(ts)} | {n_t1} | {n_t2} | "
            f"{p50_ttft:.0f} | {p99_ttft:.0f} | "
            f"{p50_inter:.1f} | {p99_inter:.1f} | "
            f"{p99_wall:.1f} | "
            f"{thresholds.stall_threshold_s:.0f} | "
            f"{thresholds.hard_cap_s:.0f} |"
        )

    lines.append("")
    lines.append("## Recommended `stall_defaults.py` (M11a-2 closeout)")
    lines.append("")
    lines.append("Append these entries to ``RECOMMENDED_STALL_THRESHOLDS_BY_MODEL``:")
    lines.append("")
    lines.append("```python")
    for model in models:
        ts = by_model.get(model, [])
        clean = [t for t in ts if t.error is None]
        if not clean:
            continue
        thr = derive_thresholds(ts, rubric=manifest.rubric)
        lines.append(
            f'    "{model}": StallThresholds('
            f"stall_threshold_s={thr.stall_threshold_s:.0f}, "
            f"hard_cap_s={thr.hard_cap_s:.0f}),"
        )
    lines.append("```")
    lines.append("")
    lines.append("## Errors")
    lines.append("")
    errored = [t for t in trials if t.error is not None]
    if not errored:
        lines.append("No errored trials.")
    else:
        for t in errored:
            lines.append(
                f"- `{t.model}` / `{t.question_id}` "
                f"[{t.tier} trial {t.trial_idx}]: {t.error}"
            )
    lines.append("")
    return "\n".join(lines) + "\n"


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _max_or_zero(values: list[float]) -> float:
    return float(max(values)) if values else 0.0


# ---------------------------------------------------------------------- #
# Metadata helper
# ---------------------------------------------------------------------- #

def _run_metadata(*,
                  suite: SuiteManifest,
                  models: list[str],
                  mode: str,
                  ollama_base: Optional[str],
                  tier1: bool,
                  tier2: bool,
                  tier1_trials: int,
                  tier2_trials: int) -> dict[str, Any]:
    """Build the metadata JSON for this run."""
    git_commit = "unknown"
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            git_commit = out.stdout.strip()
    except Exception:  # pragma: no cover — best-effort
        pass

    return {
        "harness_version": HARNESS_VERSION,
        "suite": suite.suite,
        "suite_version": suite.suite_version,
        "suite_hash": suite.suite_hash,
        "suite_released": suite.released,
        "rubric": suite.rubric,
        "models": list(models),
        "mode": mode,
        "ollama_base": ollama_base,
        "tier1_enabled": tier1,
        "tier2_enabled": tier2,
        "tier1_trials": tier1_trials,
        "tier2_trials": tier2_trials,
        "git_commit": git_commit,
        "started_at": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# ---------------------------------------------------------------------- #
# Main runner
# ---------------------------------------------------------------------- #

def run_bench(*,
              models: list[str],
              questions_dir: Path,
              output_dir: Path,
              mode: str,                  # "dry-run" | "live"
              ollama_base: Optional[str],
              tier1: bool,
              tier2: bool,
              tier1_trials: int,
              tier2_trials: int,
              id_filter: Optional[set[str]],
              smoke: bool,
              ) -> int:
    """Execute the bench. Returns the count of trials run.

    In ``--smoke`` mode the harness reduces the cohort to 1
    question per tier × 2 models × 1 trial — enough to validate
    end-to-end without significant cloud spend.
    """
    suite = load_suite_manifest(questions_dir)
    questions = load_questions(
        questions_dir, id_filter=id_filter, require_oracle=False,
    )
    if not questions:
        log.error("no questions matched the filters; nothing to run")
        return 0

    if smoke:
        # 1 question per tier; the first standalone + the first council.
        first_standalone = next(
            (q for q in questions if q.tier == "standalone"), None,
        )
        first_council = next(
            (q for q in questions if q.tier == "council"), None,
        )
        smoke_qs: list[BenchQuestion] = []
        if tier1 and first_standalone is not None:
            smoke_qs.append(first_standalone)
        if tier2 and first_council is not None:
            smoke_qs.append(first_council)
        questions = smoke_qs
        models = models[:2]
        tier1_trials = min(tier1_trials, 1)
        tier2_trials = min(tier2_trials, 1)

    metadata = _run_metadata(
        suite=suite, models=models, mode=mode,
        ollama_base=ollama_base,
        tier1=tier1, tier2=tier2,
        tier1_trials=tier1_trials, tier2_trials=tier2_trials,
    )
    estimate = estimate_stall_cost(
        questions, models,
        tier1_trials=tier1_trials, tier2_trials=tier2_trials,
        tier1=tier1, tier2=tier2,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(
        json.dumps(
            {**metadata,
             "questions": [q.id for q in questions],
             "estimate": estimate},
            indent=2,
        ),
        encoding="utf-8",
    )
    trials_path = output_dir / "trials.jsonl"
    # Truncate any prior run's jsonl so this run's report is clean.
    trials_path.write_text("", encoding="utf-8")

    # Banner.
    print(f"==== stall-bench v{HARNESS_VERSION} | "
          f"suite={suite.suite}@{suite.suite_version} "
          f"(hash {suite.suite_hash[:12]}) | mode={mode} ====",
          flush=True)
    print(f"     {estimate['summary_line']}", flush=True)
    print(f"     output: {output_dir}", flush=True)
    if smoke:
        print(f"     [smoke mode: {len(questions)} q × "
              f"{len(models)} m × ≤{max(tier1_trials, tier2_trials)} trials]",
              flush=True)
    print(flush=True)

    # Iterate trials.
    trials: list[StallTrial] = []
    n_done = 0
    expected = sum([
        (sum(1 for q in questions if q.tier == "standalone")
         * len(models) * tier1_trials) if tier1 else 0,
        (sum(1 for q in questions if q.tier == "council")
         * len(models) * tier2_trials) if tier2 else 0,
    ])

    for q in questions:
        if q.tier == "standalone" and not tier1:
            continue
        if q.tier == "council" and not tier2:
            continue

        trials_this = tier1_trials if q.tier == "standalone" else tier2_trials
        for model in models:
            # Build the timing-capture wrapper. For dry-run, wrap a
            # stub. For live, wrap a real ChatClient.
            for trial_idx in range(trials_this):
                n_done += 1
                print(
                    f"  [{n_done}/{expected}] {q.tier:10s} "
                    f"{q.id} × {model} (trial {trial_idx + 1})... ",
                    end="", flush=True,
                )

                if mode == "dry-run":
                    inner = _DryRunStreamingClient(
                        tokens=30,
                        ttft_ms=80.0,
                        gap_ms=25.0,
                        jitter_token=20, jitter_ms=150.0,
                    )
                else:
                    inner = _make_live_chat_client(model, ollama_base or "")

                capture = TimingCaptureChat(inner, model=model)

                if q.tier == "standalone":
                    trial = run_standalone_trial(
                        q, model, trial_idx,
                        capture=capture, suite_hash=suite.suite_hash,
                    )
                else:
                    trial = run_council_trial(
                        q, model, trial_idx,
                        capture=capture, suite_hash=suite.suite_hash,
                    )

                trials.append(trial)
                append_trial(trials_path, trial)

                if trial.error:
                    print(f"ERROR ({trial.error[:80]})", flush=True)
                else:
                    print(
                        f"ok (wall {trial.wall_s:.1f}s, "
                        f"{trial.num_chat_calls} calls, "
                        f"p99 inter {trial.inter_token_p99_ms:.0f}ms)",
                        flush=True,
                    )

    # Render report.
    report = render_report(
        trials=trials, manifest=suite, models=models,
        mode=mode, ollama_base=ollama_base,
    )
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    print(flush=True)
    print(f"==== done. report at {output_dir / 'report.md'} ====", flush=True)
    return len(trials)


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stall_bench",
        description=("Consultancy Skill-Eval — stall sub-protocol. "
                     "Measures per-model streaming-token cadence + "
                     "derives recommended (stall_threshold_s, hard_cap_s)."),
    )

    mode_grp = p.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--dry-run", action="store_true",
                          help="Stub clients; validates harness "
                               "end-to-end without cloud spend.")
    mode_grp.add_argument("--live", action="store_true",
                          help="Hits the live Ollama-Pro proxy. "
                               "Requires --accept-cost.")

    p.add_argument("--accept-cost", action="store_true",
                   help="Acknowledge the cost estimate for a "
                        "--live run. Without this --live exits 2.")

    p.add_argument("--models",
                   default=",".join(DEFAULT_MODELS),
                   help="Comma-separated model tags. "
                        "Default: the M11b-mlang cohort + "
                        "gemini-3-flash-preview.")

    p.add_argument("--ollama-base", default=DEFAULT_OLLAMA_BASE,
                   help="Ollama-compatible base URL. Required "
                        "for --live; ignored for --dry-run.")

    tier_grp = p.add_mutually_exclusive_group()
    tier_grp.add_argument("--tier1", action="store_true",
                          help="Run only Tier 1 (standalone).")
    tier_grp.add_argument("--tier2", action="store_true",
                          help="Run only Tier 2 (council).")
    tier_grp.add_argument("--both", action="store_true",
                          help="Run both tiers (default).")

    p.add_argument("--trials-tier1", type=int, default=DEFAULT_TIER1_TRIALS,
                   help=f"Trials per (question × model) for Tier 1. "
                        f"Default {DEFAULT_TIER1_TRIALS}.")
    p.add_argument("--trials-tier2", type=int, default=DEFAULT_TIER2_TRIALS,
                   help=f"Trials per (question × model) for Tier 2. "
                        f"Default {DEFAULT_TIER2_TRIALS}.")

    p.add_argument("--output-dir", default=None,
                   help="Output directory for metadata + JSONL + "
                        "report. Default: "
                        "benchmarks/consultants/results/<YYYY-MM-DD>/stall/.")

    p.add_argument("--questions-dir",
                   default="benchmarks/consultants/questions/stall",
                   help="Directory containing the stall SUITE.md + "
                        "question files.")

    p.add_argument("--id",
                   help="Comma-separated question ids to include. "
                        "Default: all.")

    p.add_argument("--smoke", action="store_true",
                   help="Smoke mode: 1 question per tier × 2 models "
                        "× 1 trial. End-to-end validation at "
                        "minimal spend.")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry. Returns process exit code."""
    parser = _build_argparser()
    args = parser.parse_args(argv)

    # Mode selection.
    mode = "dry-run" if args.dry_run else "live"

    # Tier selection.
    if args.tier1:
        tier1, tier2 = True, False
    elif args.tier2:
        tier1, tier2 = False, True
    else:
        tier1, tier2 = True, True   # default + --both

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    id_filter = {x.strip() for x in args.id.split(",")} if args.id else None

    # Cost estimate gate for --live.
    if mode == "live":
        questions_dir = Path(args.questions_dir)
        if not questions_dir.is_dir():
            print(f"questions dir not found: {questions_dir}", file=sys.stderr)
            return 1
        suite = load_suite_manifest(questions_dir)
        questions = load_questions(
            questions_dir, id_filter=id_filter, require_oracle=False,
        )
        estimate = estimate_stall_cost(
            questions, models,
            tier1_trials=args.trials_tier1,
            tier2_trials=args.trials_tier2,
            tier1=tier1, tier2=tier2,
        )
        if not args.accept_cost:
            print("==== live run cost estimate ====", file=sys.stderr)
            print(f"     suite: {suite.suite}@{suite.suite_version} "
                  f"({suite.suite_hash[:8]})", file=sys.stderr)
            print(f"     {estimate['summary_line']}", file=sys.stderr)
            print("     re-run with --accept-cost to proceed.",
                  file=sys.stderr)
            return 2

    # Output directory.
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        today = _dt.date.today().isoformat()
        output_dir = Path(
            f"benchmarks/consultants/results/{today}/stall",
        )

    # Pre-warm the logger so the dry-run path doesn't get a
    # surprise "no handlers" warning.
    logging.basicConfig(
        level=os.environ.get("STALL_BENCH_LOGLEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    n = run_bench(
        models=models,
        questions_dir=Path(args.questions_dir),
        output_dir=output_dir,
        mode=mode,
        ollama_base=args.ollama_base if mode == "live" else None,
        tier1=tier1, tier2=tier2,
        tier1_trials=args.trials_tier1,
        tier2_trials=args.trials_tier2,
        id_filter=id_filter,
        smoke=args.smoke,
    )
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
