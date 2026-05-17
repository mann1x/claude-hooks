#!/usr/bin/env python3
"""M11b coder skill-eval runner.

Drives the canonical coder-suite v1.0 (see
``benchmarks/consultants/questions/coder/SUITE.md``) against one or
more candidate models for the M10 ``coder`` role. Two phases:

- ``--dry-run`` — uses a stub ChatClient + stub run_loop that
  simulates a correct submission. Validates the harness pipeline
  end-to-end without cloud spend. Fast (~5 s for 32 trials).
- ``--live --accept-cost`` — uses the real
  ``claude_hooks.get_advice.chat_client.make_agent_chat_client``
  against the configured Ollama proxy (default
  ``http://192.168.178.2:11433``). Real cloud calls; the summary
  cost line prints once at the start so the run's token footprint
  is visible upfront.

Per the Consultancy Skill-Eval Protocol (docs/consultants-skill-eval-
protocol.md):

- Suite version + content hash are recorded in every results file
  so baselines are comparable across runs.
- Trial JSON is append-only — Ctrl-C mid-run keeps everything
  produced so far.
- The decision rubric (pass_rate ≥ 0.70, quality ≥ 3.5,
  tie-broken by median_tokens) is enforced by ``analyze.py`` against
  the recorded trials; this script only collects data.

CLI:

    coder_bench.py --dry-run \\
        --models kimi-k2.6:cloud,qwen3-next:cloud \\
        --output-dir results/2026-05-16/coder

    coder_bench.py --live --accept-cost \\
        --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud \\
        --ollama-base http://192.168.178.2:11433 \\
        --output-dir results/2026-05-16/coder \\
        --judge-model kimi-k2.6:cloud
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Add repo root to sys.path so ``consultants`` and
# ``benchmarks.consultants`` import cleanly when this script is run
# from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import (  # noqa: E402
    HARNESS_VERSION, BenchQuestion, CoderTrial, SuiteManifest,
    append_trial, build_judge_messages,
    build_post_test_judge_messages, build_robustness_judge_messages,
    count_code_lines,
    estimate_cost, judge_lang_for_path, load_questions,
    load_suite_manifest, make_dry_run_loop_runner, measure_complexity,
    parse_constraint_tests, parse_judge_response,
    run_pytest_against_sandbox,
)

log = logging.getLogger("benchmarks.consultants.coder_bench")


DEFAULT_MODELS = [
    "kimi-k2.6:cloud",
    "qwen3-next:cloud",
    "glm-5.1:cloud",
    "gemma4:31b-cloud",
]
DEFAULT_OLLAMA_BASE = "http://192.168.178.2:11433"
DEFAULT_JUDGE_MODEL = "kimi-k2.6:cloud"
DEFAULT_QUESTIONS_DIR = (
    _REPO_ROOT / "benchmarks" / "consultants" / "questions" / "coder"
)


# ============================================================== #
# Provenance helpers
# ============================================================== #

def _git_commit() -> str:
    """Return the current HEAD commit short hash, or '' on error."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        return out.decode("utf-8").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _run_metadata(*, suite: SuiteManifest, models: list[str],
                  mode: str, ollama_base: Optional[str],
                  judge_model: Optional[str]) -> dict:
    """Reproducibility metadata for the run header. Every results
    file starts with one of these so the analyzer can confirm
    suite version + harness version + git commit before scoring.
    """
    return {
        "harness_version": HARNESS_VERSION,
        "suite": suite.suite,
        "suite_version": suite.suite_version,
        "suite_hash": suite.suite_hash,
        "suite_released": suite.released,
        "manifest": list(suite.manifest),
        "rubric": dict(suite.rubric),
        "models": list(models),
        "mode": mode,                # "dry-run" | "live"
        "ollama_base": ollama_base,
        "judge_model": judge_model,
        "git_commit": _git_commit(),
        "host": socket.gethostname(),
        "run_started_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


# ============================================================== #
# Trial runner — common path for both dry-run and live
# ============================================================== #

def _build_trial_sandbox(output_dir: Path,
                        question: BenchQuestion, model: str,
                        idx: int) -> Path:
    """Produce a per-trial sandbox dir under
    ``<output_dir>/trials/<NN>-<model>-<qid>/``. The slugged model
    name keeps the path filesystem-safe.
    """
    model_slug = model.replace(":", "_").replace("/", "_")
    trial_id = f"{idx:03d}-{model_slug}-{question.id}"
    sandbox = output_dir / "trials" / trial_id
    sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def _judge_trial_quality(*, judge_chat_client, judge_model: str,
                         task: str, sandbox: Path,
                         sandbox_path: str) -> tuple[Optional[float], str]:
    """Call the judge LLM on the produced code; return
    ``(score, rationale)``. ``score`` is None when:

    - judge_chat_client is None (no judging configured)
    - the produced file doesn't exist
    - the judge response is unparseable
    - the judge returns empty content (one retry, then give up)

    Discriminating ``rationale`` tags help post-mortems tell the
    failure modes apart (the 2026-05-16 M11b full run had a
    ``(None, '')`` trial whose root cause was unrecoverable because
    the rationale was empty — that observability gap is closed
    here).
    """
    if judge_chat_client is None:
        return None, ""
    code_path = sandbox / sandbox_path
    if not code_path.is_file():
        return None, "code file not produced"
    try:
        code = code_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"could not read produced file: {e}"
    language, fence = judge_lang_for_path(sandbox_path)
    msgs = build_judge_messages(task, code, language=language, fence=fence)

    def _call_once() -> str:
        try:
            resp = judge_chat_client.chat({
                "model": judge_model,
                "messages": msgs,
                "stream": False,
            })
        except Exception as e:
            log.exception("judge call raised; treating as no-score")
            raise RuntimeError(f"judge call raised: {e}") from e
        if not isinstance(resp, dict):
            return ""
        choices = resp.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""

    # First attempt.
    try:
        text = _call_once()
    except RuntimeError as e:
        return None, str(e)
    # Retry once on empty content — cheap insurance against a
    # transient judge silence (observed once-in-32 on the
    # 2026-05-16 M11b full run, kimi judging kimi).
    if not text.strip():
        try:
            text = _call_once()
        except RuntimeError as e:
            return None, f"judge empty then raised: {e}"
        if not text.strip():
            return None, (
                f"judge returned empty content twice "
                f"(model={judge_model})"
            )
    score, rationale = parse_judge_response(text)
    if score is None and not rationale:
        # parse_judge_response returns ("", "") only when text was
        # empty after strip — already handled above. So a non-None
        # text that yields no score should always leave the first
        # 200 chars in rationale; defend against drift anyway.
        rationale = (
            f"judge text unparseable "
            f"(first 200 chars: {text.strip()[:200]!r})"
        )
    return score, rationale


def _audit_judge_trial(*, judge_chat_client, judge_model: str,
                       task: str, sandbox: Path,
                       sandbox_path: str,
                       passes_algorithm: bool,
                       test_results: dict,
                       constraint_names: set,
                       language: str, fence: str,
                       ) -> tuple[Optional[float], str, str, str]:
    """v1.0.1 audit judge: a second judge that fires on every compiled
    trial, with one of two prompts depending on ``passes_algorithm``.

    Returns ``(score, rationale, target_test, mode)``:
      score        — float 1-5 from the judge, None on any failure
      rationale    — the judge's one-line justification or an error msg
      target_test  — post_test mode: the failing test name shown to
                     the judge. robustness mode: empty string.
      mode         — "post_test" / "robustness" / "skipped"

    Modes:
      passes_algorithm=False AND a failing non-constraint test exists
        → "post_test" mode: judge sees failure context, rates
          edge-case awareness.
      passes_algorithm=True (or alg=False but no per-test data) and
      the code file exists
        → "robustness" mode: judge sees only the code, rates
          robustness ("name ONE plausible edge case the visible
          tests don't cover").
      otherwise
        → "skipped" mode: no judge call made; score=None.

    Always exactly one judge call (or zero if mode='skipped'), so the
    per-trial cost is bounded.

    Same retry-once-on-empty-content insurance as the read-only judge
    in ``_judge_trial_quality``.
    """
    if judge_chat_client is None or not judge_model:
        return None, "no audit judge configured", "", "skipped"
    code_path = sandbox / sandbox_path
    if not code_path.is_file():
        return None, "code file not produced", "", "skipped"
    try:
        code = code_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"could not read produced file: {e}", "", "skipped"

    # Mode selection.
    target_test = ""
    failing_msg = ""
    if not passes_algorithm:
        for name, r in test_results.items():
            if name in constraint_names:
                continue
            if r.get("status") not in ("passed", "skipped"):
                target_test = name
                failing_msg = r.get("msg") or ""
                break
        if target_test:
            mode = "post_test"
        else:
            # alg=False but junit gave us no per-test data (parse
            # failure / subprocess-exit-code fallback). Treat as
            # robustness mode — at least we can ask "is this code
            # robust?" without specific failure context.
            mode = "robustness"
    else:
        mode = "robustness"

    if mode == "post_test":
        msgs = build_post_test_judge_messages(
            task=task, code=code,
            failing_test_name=target_test, failing_msg=failing_msg,
            language=language, fence=fence,
        )
    else:  # robustness
        msgs = build_robustness_judge_messages(
            task=task, code=code,
            language=language, fence=fence,
        )

    def _call_once() -> str:
        try:
            resp = judge_chat_client.chat({
                "model": judge_model,
                "messages": msgs,
                "stream": False,
            })
        except Exception as e:
            log.exception("audit judge call raised; treating as no-score")
            raise RuntimeError(f"audit judge raised: {e}") from e
        if not isinstance(resp, dict):
            return ""
        choices = resp.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        return (choices[0].get("message") or {}).get("content") or ""

    try:
        text = _call_once()
    except RuntimeError as e:
        return None, str(e), target_test, mode
    if not text.strip():
        try:
            text = _call_once()
        except RuntimeError as e:
            return None, f"audit judge empty then raised: {e}", target_test, mode
        if not text.strip():
            return None, (
                f"audit judge returned empty content twice "
                f"(model={judge_model})"
            ), target_test, mode
    score, rationale = parse_judge_response(text)
    if score is None and not rationale:
        rationale = (
            f"audit judge text unparseable "
            f"(first 200 chars: {text.strip()[:200]!r})"
        )
    return score, rationale, target_test, mode


def _run_one_trial(*,
                   question: BenchQuestion, model: str,
                   idx: int,
                   coder_chat_client,
                   loop_runner,
                   judge_chat_client,
                   judge_model: Optional[str],
                   audit_judge_chat_client,
                   audit_judge_model: Optional[str],
                   output_dir: Path,
                   pytest_python: str) -> CoderTrial:
    """Execute one (question × model) trial.

    Returns a populated CoderTrial. Never raises — failures are
    recorded as ``error`` strings.
    """
    from consultants.engine.coder import coder_node
    from consultants.engine.state_v2 import CoderTaskItem

    trial = CoderTrial(
        question_id=question.id,
        tier=question.tier,
        model=model,
        timestamp=datetime.datetime.utcnow().isoformat() + "Z",
    )
    sandbox = _build_trial_sandbox(output_dir, question, model, idx)
    trial.sandbox_dir = str(sandbox)

    # The coder_node runs inside a per-session sandbox at
    # <cwd>/.claude-hooks/consultants/<sid>/coder-out/. We point
    # cwd at the trial dir and pass a synthetic sid so the produced
    # file lands directly under <sandbox>/.claude-hooks/...; then
    # we re-root the oracle's $CODER_SANDBOX to that produced dir.
    sid = "bench"
    bench_cwd = sandbox  # sandbox is the cwd for this trial
    produced_dir = (
        bench_cwd / ".claude-hooks" / "consultants" / sid / "coder-out"
    )

    # Track per-iteration token usage via the recorder shim. We
    # don't need the full recorder — just a tiny collector.
    iterations = 0
    tokens_prompt = 0
    tokens_completion = 0

    class _Collector:
        def record_node(self, **kw):
            pass

        def record_llm(self, **kw):
            nonlocal iterations, tokens_prompt, tokens_completion
            iterations += 1
            tokens_prompt += int(kw.get("prompt_tokens") or 0)
            tokens_completion += int(kw.get("completion_tokens") or 0)

        def record_tool(self, **kw):
            pass

    rec = _Collector()
    task_item = CoderTaskItem(
        task=question.task,
        path=question.sandbox_path,
        lane_idx=0,
        parent_round=1,
    )
    state = {
        "coder_task_item": task_item,
        "lane_idx": 0,
        "question": question.task,
        "plan": "",          # no plan in bench — the task IS the plan
        "research": [],      # no researcher findings
    }
    # v1.0.1: reset per-trial so trial.inference_s captures only the
    # successful-attempt inference time on this trial's calls. wall_s
    # still measures the full wall (including any ChatClient retry
    # backoffs) for the operator-facing "elapsed" number.
    try:
        coder_chat_client.reset_inference_timer()
    except AttributeError:
        # Old ChatClient without the timer — bench still runs, just
        # leaves trial.inference_s at 0.0. Should never hit on the
        # current claude-hooks build.
        pass
    t0 = time.monotonic()
    try:
        result = coder_node(
            state,
            chat_client=coder_chat_client,
            grounding_msgs=[],
            model=model,
            cwd=str(bench_cwd),
            sid=sid,
            think="high",
            loop_runner=loop_runner,
            recorder=rec,
        )
    except Exception as e:
        log.exception("trial %s × %s raised", question.id, model)
        trial.wall_s = time.monotonic() - t0
        trial.inference_s = float(
            getattr(coder_chat_client, "total_inference_s", 0.0) or 0.0
        )
        trial.iterations = iterations
        trial.tokens_prompt = tokens_prompt
        trial.tokens_completion = tokens_completion
        trial.error = f"{type(e).__name__}: {e}"
        return trial
    trial.wall_s = time.monotonic() - t0
    trial.inference_s = float(
        getattr(coder_chat_client, "total_inference_s", 0.0) or 0.0
    )
    trial.iterations = iterations
    trial.tokens_prompt = tokens_prompt
    trial.tokens_completion = tokens_completion

    # Extract artifact + files-written summary.
    artifacts = result.get("coder_artifacts") or []
    if not artifacts:
        trial.error = "no coder_artifacts in result"
        return trial
    art = artifacts[0]
    trial.files_written = list(getattr(art, "files", []) or [])
    if art.error:
        trial.error = art.error

    # Locate the produced file. The coder may have ignored our
    # suggested path; per the suite contract the task asks for an
    # exact filename, so we look for that path. If the model wrote
    # to a different name, the oracle will report "module not
    # importable" — which is the intended consequence.
    code_file = produced_dir / question.sandbox_path
    if code_file.is_file():
        # Pre-compile-style parse check. Python: py_compile catches
        # ``SyntaxError`` before pytest runs (fast-fail). Non-
        # Python: skip — the oracle's ``compile_and_run`` invokes
        # the real toolchain (rustc / gcc / g++ / go build /
        # dotnet) which produces an authoritative diagnostic.
        # Determined by sandbox_path's extension, not by language
        # name, so a future suite using ``.cs`` / ``.rs`` / ``.go``
        # / ``.c`` / ``.cpp`` files routes correctly.
        suffix = code_file.suffix.lower()
        if suffix == ".py":
            trial.compiles = True
            try:
                import py_compile
                py_compile.compile(str(code_file), doraise=True)
            except Exception as e:
                trial.compiles = False
                trial.error = f"compile failed: {e}"
        else:
            # Non-Python: let the oracle's compile-and-run be the
            # source of truth. Set compiles=True so the oracle
            # runs; oracle failure → trial.passes_tests=False with
            # the compiler stderr captured.
            trial.compiles = True
        trial.code_lines = count_code_lines(code_file)
        trial.complexity = measure_complexity(code_file)
    else:
        trial.compiles = False

    # Oracle pytest run — only when something compiled. A non-
    # compiling submission can't pass tests, so we skip pytest
    # entirely (saves ~1 s per trial across 32 trials = 30+ s).
    if trial.compiles:
        oracle_result = run_pytest_against_sandbox(
            question.oracle_path,
            produced_dir,
            python_executable=pytest_python,
            timeout_s=60.0,
        )
        # v1.0.1 two-axis split: parse the oracle file to identify
        # which tests are constraint-marked, then compute algorithm
        # vs constraint pass-rates from the per-test junit data.
        constraint_names = parse_constraint_tests(question.oracle_path)
        trial.test_results = oracle_result.test_results
        # Split per-test results by category.
        alg_results = {
            n: r for n, r in oracle_result.test_results.items()
            if n not in constraint_names
        }
        con_results = {
            n: r for n, r in oracle_result.test_results.items()
            if n in constraint_names
        }
        # passes_algorithm = every non-constraint test passed.
        # An empty alg_results dict (e.g. trial didn't compile,
        # though we're inside the compiles branch) yields True via
        # all([]) — but compiles=True here so we'd get test data
        # OR a junit-parse failure. The latter sets passes_tests
        # to oracle_result.passed (back-compat) to avoid a false
        # positive.
        if alg_results:
            trial.passes_algorithm = all(
                r["status"] == "passed" for r in alg_results.values()
            )
        else:
            # No per-test data parsed (junit parse failure or
            # subprocess timeout before any test ran) — fall back
            # to the binary subprocess exit code.
            trial.passes_algorithm = oracle_result.passed
        if con_results:
            trial.passes_constraints = all(
                r["status"] == "passed" for r in con_results.values()
            )
            trial.constraint_violations = sorted(
                n for n, r in con_results.items()
                if r["status"] != "passed"
            )
        else:
            # No constraint tests in this oracle (the medium tier
            # mostly relies on existence checks for Python and
            # nothing for C/Go). Vacuously "passed".
            trial.passes_constraints = True
            trial.constraint_violations = []
        # passes_tests is the back-compat alias = passes_algorithm
        # (the practical "does the code work?" signal). v1.0
        # callers reading this field continue to get a useful
        # answer; the report renderer is opt-in for the two-axis
        # surface.
        trial.passes_tests = trial.passes_algorithm
        # Keep stdout truncated so the JSON stays bounded — but wide
        # enough to fit the full pytest failure preamble for hard
        # questions. The 2026-05-16 coder_mlang v1 run kept this at
        # 2000 chars; mid-run analysis showed multiple very_hard
        # trials had the FAILURES header consume the visible window
        # before the assertion message landed. Bumped to 8000 (head)
        # + 8000 (tail) so both the first failed-test summary AND
        # the trailing FAILED summary survive.
        out = oracle_result.stdout
        if len(out) > 16000:
            kept = out[:8000] + (
                f"\n...[{len(out) - 16000} chars elided]...\n"
            ) + out[-8000:]
        else:
            kept = out
        trial.test_output = (
            kept
            + ("\n--STDERR--\n" + oracle_result.stderr[-2000:]
               if oracle_result.stderr else "")
        )

        # Judge call — only on trials that compiled (no point
        # judging code that can't run). Skip if no judge model.
        if judge_chat_client is not None and judge_model:
            score, rationale = _judge_trial_quality(
                judge_chat_client=judge_chat_client,
                judge_model=judge_model,
                task=question.task,
                sandbox=produced_dir,
                sandbox_path=question.sandbox_path,
            )
            trial.quality_score = score
            trial.quality_rationale = rationale

        # v1.0.1 audit judge — fires on EVERY compiled trial when a
        # separate judge model is configured. Uses two prompt modes:
        #   passes_algorithm=False → "post_test" prompt (sees failure)
        #   passes_algorithm=True  → "robustness" prompt (no failure)
        # The cross-judge cross-model coverage gives us a second
        # opinion on every trial — addresses the kimi-as-sole-judge
        # bias (kimi rating its own coder output would otherwise have
        # no independent check) and captures latent bugs in passing
        # code (minimax-style "robust algorithm masking buggy pivot").
        # See ``_audit_judge_trial`` docstring for the rubric details.
        if (
            audit_judge_chat_client is not None
            and audit_judge_model
        ):
            language, fence = judge_lang_for_path(question.sandbox_path)
            a_score, a_rat, a_target, a_mode = _audit_judge_trial(
                judge_chat_client=audit_judge_chat_client,
                judge_model=audit_judge_model,
                task=question.task,
                sandbox=produced_dir,
                sandbox_path=question.sandbox_path,
                passes_algorithm=trial.passes_algorithm,
                test_results=trial.test_results,
                constraint_names=constraint_names,
                language=language, fence=fence,
            )
            trial.quality_audit_score = a_score
            trial.quality_audit_rationale = a_rat
            trial.quality_audit_judge_model = audit_judge_model
            trial.quality_audit_target_test = a_target
            trial.quality_audit_mode = a_mode
    return trial


# ============================================================== #
# Live-mode ChatClient factory
# ============================================================== #

def _make_live_clients(models: list[str], ollama_base: str,
                       judge_model: Optional[str],
                       audit_judge_model: Optional[str] = None,
                       ) -> tuple[dict, Any, Any]:
    """Build per-model ChatClients via
    ``make_agent_chat_client``. Returns
    ``(coder_clients_by_model, judge_client_or_None,
    audit_judge_client_or_None)``.

    Lazy import so dry-run mode works in envs without the full
    claude_hooks stack.

    The audit judge gets its own ChatClient so retry counters +
    inference-time accounting stay separate from the read-only
    judge's. Pick a DIFFERENT model from ``judge_model`` to get
    cross-judge cross-model robustness — if both judges are the
    same model, the cross-judge variance signal collapses.
    """
    from claude_hooks.get_advice.chat_client import make_agent_chat_client
    coder_clients: dict[str, Any] = {}
    for m in models:
        coder_clients[m] = make_agent_chat_client(m, ollama_base)
    judge_client = None
    if judge_model:
        judge_client = make_agent_chat_client(judge_model, ollama_base)
    audit_judge_client = None
    if audit_judge_model:
        audit_judge_client = make_agent_chat_client(
            audit_judge_model, ollama_base,
        )
    return coder_clients, judge_client, audit_judge_client


# ============================================================== #
# Dry-run client factory — same shape as live, but with a stub
# ChatClient that returns sensible token counts + a stub run_loop
# that synthesizes a "I wrote the file" trace.
# ============================================================== #

class _DryRunChatClient:
    """Stub ChatClient — never actually issues HTTP calls. Returns
    a tiny response shape with token counts so the harness's
    metrics path produces real numbers."""
    def __init__(self, *, prompt_tokens: int = 500,
                 completion_tokens: int = 200):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def chat(self, payload, *, think=False):
        return {
            "choices": [{
                "message": {"role": "assistant",
                             "content": "stub response"},
            }],
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            },
        }


# Hand-written "correct" submissions used by the dry-run path so
# every oracle's pytest passes when --dry-run is active. The
# dry-run pretends the model wrote these files. Useful for
# validating the bench end-to-end without cloud spend.
_DRY_RUN_SUBMISSIONS: dict[str, str] = {
    "trivial-01-truncate": (
        "def truncate(s: str, n: int) -> str:\n"
        "    if n <= 0:\n"
        "        return ''\n"
        "    return s[:n]\n"
    ),
    "trivial-02-strlen": (
        "def strlen(s: str) -> int:\n"
        "    count = 0\n"
        "    for _ in s:\n"
        "        count += 1\n"
        "    return count\n"
    ),
    "easy-01-dedupe": (
        "def dedupe(items):\n"
        "    seen = set()\n"
        "    out = []\n"
        "    for x in items:\n"
        "        if x not in seen:\n"
        "            seen.add(x)\n"
        "            out.append(x)\n"
        "    return out\n"
    ),
    "easy-02-fib": (
        "def fib(n: int) -> int:\n"
        "    if n < 0:\n"
        "        raise ValueError('n must be non-negative')\n"
        "    if n < 2:\n"
        "        return n\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n - 1):\n"
        "        a, b = b, a + b\n"
        "    return b\n"
    ),
    "medium-01-balance": (
        "def is_balanced(s: str) -> bool:\n"
        "    pairs = {')': '(', ']': '[', '}': '{'}\n"
        "    opens = set(pairs.values())\n"
        "    stack = []\n"
        "    for ch in s:\n"
        "        if ch in opens:\n"
        "            stack.append(ch)\n"
        "        elif ch in pairs:\n"
        "            if not stack or stack[-1] != pairs[ch]:\n"
        "                return False\n"
        "            stack.pop()\n"
        "    return not stack\n"
    ),
    "medium-02-prime-length": (
        "def prime_length(s: str) -> bool:\n"
        "    n = len(s)\n"
        "    if n < 2:\n"
        "        return False\n"
        "    if n < 4:\n"
        "        return True\n"
        "    if n % 2 == 0:\n"
        "        return False\n"
        "    i = 3\n"
        "    while i * i <= n:\n"
        "        if n % i == 0:\n"
        "            return False\n"
        "        i += 2\n"
        "    return True\n"
    ),
    "hard-01-matrix-path": (
        "def min_path_sum(grid):\n"
        "    if not grid or not grid[0]:\n"
        "        raise ValueError('grid is empty')\n"
        "    m = len(grid)\n"
        "    n = len(grid[0])\n"
        "    for row in grid:\n"
        "        if len(row) != n:\n"
        "            raise ValueError('grid is jagged')\n"
        "    dp = [row[:] for row in grid]\n"
        "    for j in range(1, n):\n"
        "        dp[0][j] += dp[0][j - 1]\n"
        "    for i in range(1, m):\n"
        "        dp[i][0] += dp[i - 1][0]\n"
        "    for i in range(1, m):\n"
        "        for j in range(1, n):\n"
        "            dp[i][j] += min(dp[i - 1][j], dp[i][j - 1])\n"
        "    return dp[-1][-1]\n"
    ),
    "hard-02-digit-filter": (
        "def sum_odd_first_last(nums):\n"
        "    count = 0\n"
        "    for v in nums:\n"
        "        if v <= 10:\n"
        "            continue\n"
        "        s = str(abs(v))\n"
        "        if int(s[0]) % 2 == 1 and int(s[-1]) % 2 == 1:\n"
        "            count += 1\n"
        "    return count\n"
    ),
}


def _make_dry_run_loop_runner_for(question: BenchQuestion):
    """Return a stub loop_runner specifically for ``question`` —
    pre-loaded with the correct submission so the oracle passes.
    This is the dry-run's promise: "if the bench plumbing is
    correct, every question produces a passing trial."
    """
    code = _DRY_RUN_SUBMISSIONS.get(question.id, "# stub\n")
    return make_dry_run_loop_runner(
        file_path=question.sandbox_path, content=code,
        iterations=3, prompt_tokens=500, completion_tokens=200,
    )


# ============================================================== #
# Main runner
# ============================================================== #

def run_bench(*,
              models: list[str],
              questions_dir: Path,
              output_dir: Path,
              mode: str,                  # "dry-run" | "live"
              ollama_base: Optional[str],
              judge_model: Optional[str],
              tier_filter: Optional[set[str]],
              id_filter: Optional[set[str]],
              pytest_python: str,
              audit_judge_model: Optional[str] = None,
              commit_report: bool = False) -> int:
    """Execute the bench. Returns the count of trials run.

    When ``commit_report`` is true: after the run finishes, render
    ``report.md`` from ``trials.jsonl`` and force-add it +
    ``metadata.json`` (+ ``quota.md`` if present, but user-authored
    so not always there) to the git index via ``git add -f``. The
    actual commit is left to the operator — the bench's job is to
    make the artifacts staged + ready, not to write history.
    """
    suite = load_suite_manifest(questions_dir)
    questions = load_questions(
        questions_dir,
        tier_filter=tier_filter,
        id_filter=id_filter,
    )
    if not questions:
        log.error("no questions matched the filters; nothing to run")
        return 0
    metadata = _run_metadata(
        suite=suite, models=models, mode=mode,
        ollama_base=ollama_base, judge_model=judge_model,
    )
    if audit_judge_model:
        metadata["audit_judge_model"] = audit_judge_model
    estimate = estimate_cost(questions, models, judge_model=judge_model)
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
    # Banner.
    print(f"==== coder-bench v{HARNESS_VERSION} | "
          f"suite={suite.suite}@{suite.suite_version} "
          f"(hash {suite.suite_hash[:12]}) | mode={mode} ====",
          flush=True)
    print(f"     {estimate['summary_line']}", flush=True)
    print(f"     output: {output_dir}", flush=True)
    print(flush=True)

    # Build the clients up-front (one shot in dry-run, real factories
    # in live mode).
    coder_clients_by_model: dict
    judge_client: Any
    audit_judge_client: Any
    if mode == "dry-run":
        coder_clients_by_model = {
            m: _DryRunChatClient() for m in models
        }
        judge_client = None  # dry-run skips the judge call
        audit_judge_client = None
    else:
        if not ollama_base:
            raise SystemExit("--live requires --ollama-base")
        (coder_clients_by_model,
         judge_client,
         audit_judge_client) = _make_live_clients(
            models, ollama_base, judge_model,
            audit_judge_model=audit_judge_model,
        )

    n_done = 0
    n_total = len(questions) * len(models)
    for q_idx, q in enumerate(questions):
        for m_idx, m in enumerate(models):
            n_done += 1
            trial_idx = n_done
            print(
                f"  [{n_done}/{n_total}] {q.id} × {m} ... ",
                end="", flush=True,
            )
            t0 = time.monotonic()
            if mode == "dry-run":
                loop_runner = _make_dry_run_loop_runner_for(q)
            else:
                loop_runner = None  # let coder_node lazy-import run_loop
            try:
                trial = _run_one_trial(
                    question=q, model=m, idx=trial_idx,
                    coder_chat_client=coder_clients_by_model[m],
                    loop_runner=loop_runner,
                    judge_chat_client=judge_client,
                    judge_model=judge_model,
                    audit_judge_chat_client=audit_judge_client,
                    audit_judge_model=audit_judge_model,
                    output_dir=output_dir,
                    pytest_python=pytest_python,
                )
            except KeyboardInterrupt:
                print("INTERRUPTED", flush=True)
                return n_done - 1
            wall = time.monotonic() - t0
            # Short status line per trial.
            if trial.error:
                status = f"ERROR ({trial.error[:60]})"
            elif not trial.compiles:
                status = "no-compile"
            elif not trial.passes_tests:
                status = "tests-fail"
            else:
                qs = (
                    f", quality={trial.quality_score:.1f}"
                    if trial.quality_score is not None else ""
                )
                status = (
                    f"PASS ({trial.iterations} iters, "
                    f"{trial.tokens_prompt + trial.tokens_completion} tok"
                    f"{qs})"
                )
            print(f"{status} ({wall:.1f}s)", flush=True)
            append_trial(trials_path, trial)
    print(flush=True)
    # Always render report.md at end of run — it's cheap (small
    # file) and removes the manual ``python -m
    # benchmarks.consultants.analyze`` step that the user kept
    # tripping over. ``--commit-report`` then force-adds the
    # rendered artifact so the ``baselines.md`` row has a clickable
    # artifact next to it in git.
    report_path = output_dir / "report.md"
    try:
        from . import analyze as _analyze_mod  # local import: bench
                                               # is sometimes loaded
                                               # without analyze on path
        rendered = _analyze_mod.render_report(
            _analyze_mod.load_trials(trials_path),
            metadata=metadata,
        )
        report_path.write_text(rendered, encoding="utf-8")
        print(f"     wrote report.md ({report_path})", flush=True)
    except Exception as e:
        log.warning("could not render report.md inline: %s", e)
    if commit_report:
        _commit_report_artifacts(output_dir)
    print(f"==== done. {n_done} trials in {trials_path}. ====",
          flush=True)
    return n_done


def _commit_report_artifacts(output_dir: Path) -> None:
    """Force-add the report-side artifacts so they're staged for
    the next commit. Never touches commit history — the operator
    decides when to commit.

    Files staged (each guarded by existence):

    - ``report.md`` and ``smoke-report.md``
    - ``metadata.json`` and ``smoke-metadata.json``
    - ``quota.md`` (user-authored; optional)

    Raw trial dumps (``trials.jsonl`` / ``trials/`` / ``*.log``)
    stay ignored — they're recreatable from a re-run.
    """
    candidates = (
        "report.md", "smoke-report.md",
        "metadata.json", "smoke-metadata.json",
        "quota.md",
    )
    to_add: list[str] = []
    for name in candidates:
        path = output_dir / name
        if path.is_file():
            to_add.append(str(path))
    if not to_add:
        log.info("commit-report: no artifacts to stage in %s",
                 output_dir)
        return
    # Resolve repo root for git invocation. The bench may be run
    # from anywhere — we honor the layout under ``--output-dir``
    # rather than CWD.
    repo_root = output_dir.resolve()
    while repo_root != repo_root.parent and not (repo_root / ".git").is_dir():
        repo_root = repo_root.parent
    if not (repo_root / ".git").is_dir():
        log.warning(
            "commit-report: could not find a git repo above %s "
            "— skipping git add",
            output_dir,
        )
        return
    import subprocess
    cmd = ["git", "-C", str(repo_root), "add", "-f", *to_add]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        log.warning("commit-report: 'git' not on PATH — skipping")
        return
    except subprocess.CalledProcessError as e:
        log.warning(
            "commit-report: git add failed (exit %d): %s",
            e.returncode, (e.stderr or "").strip(),
        )
        return
    print(
        f"     commit-report: staged {len(to_add)} files via "
        f"git add -f (commit when ready)",
        flush=True,
    )


# ============================================================== #
# CLI
# ============================================================== #

def _parse_csv(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coder_bench",
        description=(
            "M11b coder skill-eval runner. See "
            "docs/consultants-skill-eval-protocol.md for the "
            "protocol context."
        ),
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Stub ChatClient + stub run_loop. Validates the "
             "harness end-to-end without cloud spend.",
    )
    mode.add_argument(
        "--live", action="store_true",
        help="Real ChatClients via make_agent_chat_client against "
             "--ollama-base. Requires --accept-cost.",
    )
    p.add_argument(
        "--accept-cost", action="store_true",
        help="Required with --live. Acknowledges the run will "
             "spend real Ollama Pro tokens (see the summary line).",
    )
    p.add_argument(
        "--models", type=_parse_csv, default=DEFAULT_MODELS,
        metavar="M1,M2,...",
        help=f"Comma-separated model list. Default: {','.join(DEFAULT_MODELS)}",
    )
    p.add_argument(
        "--ollama-base", default=DEFAULT_OLLAMA_BASE,
        help=f"Ollama proxy base URL. Default: {DEFAULT_OLLAMA_BASE}",
    )
    p.add_argument(
        "--judge-model", default=DEFAULT_JUDGE_MODEL,
        help=("Model used as the code-quality judge. Set to '' to "
              f"skip judging. Default: {DEFAULT_JUDGE_MODEL}"),
    )
    p.add_argument(
        "--audit-judge-model", default="",
        help=("v1.0.1: model used as the AUDIT judge — a second judge "
              "that fires on EVERY compiled trial with one of two "
              "prompt modes: ``post_test`` (passes_algorithm=False, "
              "judge sees failing test) or ``robustness`` (passes_"
              "algorithm=True, judge identifies a latent edge case). "
              "Gives cross-judge cross-model coverage on every trial "
              "and addresses the read-only-judge bias when the judge "
              "is also rating its own coder output. Set to '' to "
              "skip the audit judge (default). Recommended: pick a "
              "DIFFERENT model from --judge-model (e.g. judge=kimi, "
              "audit=glm)."),
    )
    p.add_argument(
        "--questions-dir", type=Path, default=DEFAULT_QUESTIONS_DIR,
        help=f"Directory of bench questions. Default: {DEFAULT_QUESTIONS_DIR}",
    )
    p.add_argument(
        "--output-dir", type=Path, default=None,
        help=("Per-run output directory. Default: "
              "benchmarks/consultants/results/<YYYY-MM-DD>/coder/"),
    )
    p.add_argument(
        "--tier", action="append", choices=("trivial", "easy", "medium", "hard"),
        help="Filter by tier (repeatable). Default: all tiers.",
    )
    p.add_argument(
        "--id", action="append",
        help="Filter by question id (repeatable). Default: all.",
    )
    p.add_argument(
        "--smoke", action="store_true",
        help="Shorthand for --tier trivial — 2 questions per model.",
    )
    p.add_argument(
        "--pytest-python", default=sys.executable,
        help=("Python interpreter used to run oracle pytest. Default: "
              "the harness's own interpreter."),
    )
    p.add_argument(
        "--commit-report", action="store_true",
        help=(
            "After the run, force-add report.md + metadata.json "
            "(+ quota.md if present) to the git index so the "
            "next commit can carry the durable artifacts next to "
            "the baselines.md row. Does NOT create a commit — "
            "operator decides when to write history."
        ),
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("CODER_BENCH_LOG", "INFO"),
        format="%(asctime)s [%(name)s] %(message)s",
    )
    args = build_parser().parse_args(argv)
    if args.live and not args.accept_cost:
        # Print the cost summary then bail. This is the "show the
        # estimate before you spend tokens" gate per the M11 design.
        suite = load_suite_manifest(args.questions_dir)
        tier_set = set(args.tier) if args.tier else None
        if args.smoke:
            tier_set = {"trivial"}
        id_set = set(args.id) if args.id else None
        qs = load_questions(
            args.questions_dir,
            tier_filter=tier_set, id_filter=id_set,
        )
        est = estimate_cost(qs, args.models, judge_model=args.judge_model)
        print("--live requires --accept-cost. Cost summary:")
        print(f"  suite: {suite.suite}@{suite.suite_version}")
        print(f"  {est['summary_line']}")
        print("Re-run with --accept-cost to proceed.")
        return 2
    mode = "live" if args.live else "dry-run"
    tier_set = set(args.tier) if args.tier else None
    if args.smoke:
        tier_set = {"trivial"}
    id_set = set(args.id) if args.id else None
    output_dir = args.output_dir or (
        _REPO_ROOT / "benchmarks" / "consultants" / "results"
        / datetime.datetime.utcnow().strftime("%Y-%m-%d") / "coder"
    )
    n = run_bench(
        models=args.models,
        questions_dir=args.questions_dir,
        output_dir=output_dir,
        mode=mode,
        ollama_base=args.ollama_base if args.live else None,
        judge_model=args.judge_model or None,
        audit_judge_model=args.audit_judge_model or None,
        tier_filter=tier_set,
        id_filter=id_set,
        pytest_python=args.pytest_python,
        commit_report=args.commit_report,
    )
    return 0 if n > 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
