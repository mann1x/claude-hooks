"""M11c tool_executor skill-eval bench — picks the recommended
model for ``cfg.roles.tool_executor.model`` based on an empirical
8-question × 4-tier suite.

Unlike the M11a stall bench (which is a pure measurement bench)
and the M11b coder bench (which writes code), this bench measures
**reading + reasoning over an existing codebase via tool calls**.
For each ``(question × model × trial_idx)`` it:

1. Builds a :class:`~consultants.engine.state_v2.ToolPlanItem` from
   the question's frontmatter.
2. Drives :func:`~consultants.engine.tool_executor.tool_executor_node`
   directly (NOT the full council) with:
   - a per-trial ChatClient pinned to the model under test;
   - the production tool stack (``survey_project``, ``list_files``,
     ``read_file``, ``glob``, ``grep``, ``recall_memory``);
   - ``cwd`` set to the question's ``fixtures_subdir`` so the
     tools' path resolution scopes to the fixture cohort, not the
     bench's working tree.
3. Captures the lane's :class:`~consultants.engine.state_v2.ToolResult`
   ``content`` (the final assistant text) + the ordered tool-call
   log via a custom recorder.
4. Runs the per-question oracle pytest with three env vars:
   ``TOOL_EXEC_OUTPUT`` (the final text), ``TOOL_EXEC_CALLS`` (the
   tool-call log as JSON), ``TOOL_EXEC_FIXTURE_DIR`` (absolute
   path to the cohort). The harness's :func:`run_pytest_against_sandbox`
   plumbs them through via ``extra_env=...``.
5. Optionally calls a judge LLM on the captured answer for a soft
   quality score (1-5 + rationale). The bench's rubric ranks
   models by pass-rate first, judge score second.

**Two-commit split**, per the M11b / M11a precedent:

- **M11c-1** (this file's first landing): harness + suite + tests
  + docs + an empty ``tool_executor_defaults.py`` scaffold.
  Dry-run-only. No cloud spend.
- **M11c-2** (separate user-confirmed commit, later): live run
  results merged in, ``tool_executor_defaults.py`` populated.

The CLI mirrors ``skill-eval coder`` and ``skill-eval stall``;
operators learn one shape and re-use it across sub-protocols.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from benchmarks.consultants.harness import (
    HARNESS_VERSION,
    BenchQuestion,
    SuiteManifest,
    ToolExecTrial,
    append_trial,
    estimate_tool_exec_cost,
    load_questions,
    load_suite_manifest,
    parse_judge_response,
    run_pytest_against_sandbox,
)

log = logging.getLogger("benchmarks.consultants.tool_executor_bench")


# ---------------------------------------------------------------------- #
# Constants
# ---------------------------------------------------------------------- #

DEFAULT_OLLAMA_BASE = "http://192.168.178.2:11433"

# Default cohort — same M11b-mlang baseline + gemini-3-flash-preview
# (the model that triggered the 2026-05-15 audit pathology), minus
# deepseek-v4-flash which is known weak at multi-tool chains in
# our trace data. The cohort can be overridden via --models.
DEFAULT_MODELS: tuple[str, ...] = (
    "glm-5.2:cloud",
    "kimi-k2.6:cloud",
    "gemma4:31b-cloud",
    "qwen3-coder-next:cloud",
    "deepseek-v4-pro:cloud",
    "gemini-3-flash-preview:cloud",
)

# Default judge model. The bench uses one judge call per
# (question × model × trial) for the soft quality score.
# ``gemma4:31b-cloud`` was picked to mirror the M11b setup; it's
# a deterministic mid-size reader, decent at grading citation
# fidelity + intent fulfillment.
DEFAULT_JUDGE_MODEL = "gemma4:31b-cloud"

# Default trials per question. Tool_executor lanes are
# deterministic enough that 1 trial captures most of the signal;
# operators can bump via --trials.
DEFAULT_TRIALS = 1

# Pytest oracle timeouts. The per-test cap is short — each test is
# a pure string check; the session cap is the safety net.
ORACLE_PER_TEST_TIMEOUT_S = 5.0
ORACLE_SESSION_TIMEOUT_S = 30.0

# Trim long answers before storing into the JSONL row. The full
# text is rarely needed post-hoc; the first ~2 KB has the
# citations + summary which is what the report renders.
FINAL_TEXT_KEEP_CHARS = 2000

# Regex matching a ``path:line`` citation in the final text.
# Conservative: only counts hits that look like a real reference
# (filename with a dot extension + line number).
_CITATION_RE = re.compile(
    r"\b[\w./-]+\.\w+:\d+\b"
)


# ---------------------------------------------------------------------- #
# Dry-run chat client + per-question prefab answers
# ---------------------------------------------------------------------- #

class _DryRunChatClient:
    """Stub :class:`ChatClient` — never issues HTTP calls. Used by
    ``--dry-run`` so the bench plumbing can be validated without
    cloud spend.

    The actual stub trace is built by :func:`_make_dry_run_loop_runner`
    below; this client only ever receives the model's final-text
    request and returns a synthetic OpenAI-shape response. Tracks
    ``total_inference_s`` so trial accounting still shows a non-zero
    number for the dry-run path.
    """

    def __init__(self, *, prompt_tokens: int = 1500,
                 completion_tokens: int = 600):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_inference_s = 0.0

    def chat(self, payload, *, think=False):
        t0 = time.monotonic()
        # Tiny sleep so wall_s ends up nonzero.
        time.sleep(0.005)
        self.total_inference_s += time.monotonic() - t0
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


def _resolve_const_line(fixture_dir: Path, filename: str,
                        const_name: str) -> int:
    """Return the line number where ``const_name`` is assigned in
    ``fixture_dir/filename``. Skips comment lines. Falls back to 0
    when not found.
    """
    p = fixture_dir / filename
    if not p.is_file():
        return 0
    src = p.read_text(encoding="utf-8", errors="replace")
    pat = re.compile(rf"^\s*{re.escape(const_name)}\s*[:=]")
    for idx, raw in enumerate(src.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        if pat.match(raw):
            return idx
    return 0


def _dry_run_recipe(question: BenchQuestion,
                    fixture_dir: Path) -> tuple[str, list[tuple[str, dict]]]:
    """Return ``(final_text, [(tool_name, args_dict), ...])`` for
    the question's dry-run trace. The dry-run loop_runner replays
    the scripted tool calls through the *real* tool callable, so
    the env-var plumbing + tool-stack integration is exercised
    end-to-end; only the model call is stubbed.

    Each recipe is hand-crafted to make the question's oracle
    pass when fed as the final_text — that's the dry-run promise:
    "if the bench plumbing is correct, every question produces a
    passing trial."

    Line numbers + dynamic strings are resolved against
    ``fixture_dir`` at runtime so a fixture re-indent doesn't flake
    the dry run (the same drift-tolerance the oracles enforce).
    """
    qid = question.id

    if qid == "trivial-01-find-symbol":
        line = _resolve_const_line(
            fixture_dir, "config.py", "DEFAULT_TIMEOUT_S",
        )
        text = (
            f"The constant `DEFAULT_TIMEOUT_S` is defined in "
            f"`config.py:{line}` with value `30.0`."
        )
        calls = [
            ("grep", {"pattern": "DEFAULT_TIMEOUT_S", "path": "."}),
            ("read_file", {"path": "config.py"}),
        ]
        return text, calls

    if qid == "trivial-02-read-section":
        text = (
            "The Configuration section in `README.md` documents "
            "three environment variables: `DEMO_HOST` (default "
            "`127.0.0.1`), `DEMO_PORT` (default `8080`), and "
            "`DEMO_TIMEOUT_S` (default `30.0`)."
        )
        calls = [("read_file", {"path": "README.md"})]
        return text, calls

    if qid == "easy-01-grep-read-chain":
        text = (
            "The function `handle_auth` lives in `auth.py` (around "
            "lines 33-50). Full body:\n\n"
            "```python\n"
            "def handle_auth(username: str, password: str, "
            "store: dict) -> str:\n"
            "    record = store.get(username)\n"
            "    if record is None:\n"
            "        raise PermissionError(\"unknown user\")\n"
            "    salt, expected_hex = record[\"password_hash\"]"
            ".split(\":\", 1)\n"
            "    actual = _hash_password(password, salt)"
            ".split(\":\", 1)[1]\n"
            "    if not secrets.compare_digest(actual, "
            "expected_hex):\n"
            "        raise PermissionError(\"bad password\")\n"
            "    token = issue_token()\n"
            "    record[\"last_login\"] = now_iso()\n"
            "    return token\n"
            "```"
        )
        calls = [
            ("grep", {"pattern": "def handle_auth", "path": "."}),
            ("read_file", {"path": "auth.py"}),
        ]
        return text, calls

    if qid == "easy-02-listfiles-glob":
        text = (
            "Files under `pkg/`: `__init__.py`, `parser.py`, "
            "`runner.py`, `serializer.py`, `validator.py`.\n\n"
            "- `pkg/parser.py` — tokenizes and parses the config "
            "DSL into a list of (key, value) AST tuples.\n"
            "- `pkg/runner.py` — orchestrates parse → validate → "
            "serialize, the package's entry point pipeline.\n"
            "- `pkg/serializer.py` — encodes the parsed AST back "
            "to UTF-8 bytes with deterministic key ordering.\n"
            "- `pkg/validator.py` — applies schema rules to the "
            "parsed AST; raises on missing required keys."
        )
        calls = [
            ("list_files", {"path": "pkg"}),
            ("read_file", {"path": "pkg/parser.py"}),
            ("read_file", {"path": "pkg/runner.py"}),
            ("read_file", {"path": "pkg/serializer.py"}),
        ]
        return text, calls

    if qid == "medium-01-multifile-audit":
        text = (
            "Found 5 TODO comments across 3 files, grouped by "
            "tag prefix:\n\n"
            "**TODO(api)** — `api.py:14` (replace placeholder "
            "lookup with a real DB call); `api.py:21` (add "
            "pagination to list_users).\n"
            "**TODO(auth)** — `auth.py:13` (validate signature "
            "not just length); `auth.py:19` (rotate signing key "
            "on every refresh).\n"
            "**TODO(cache)** — `cache.py:17` (wire eviction so "
            "the dict can't grow unbounded)."
        )
        calls = [("grep", {"pattern": "TODO", "path": "."})]
        return text, calls

    if qid == "medium-02-redundancy-test":
        text = (
            "`DEFAULT_POOL_SIZE = 50` (already confirmed in round "
            "1 at `app/config.py:42`). Echoing for the "
            "synthesizer — the value matters because the executor "
            "pool sizing must not exceed this connection budget."
        )
        # The redundancy test deliberately makes no tool calls;
        # the answer is in the prompt's why block.
        calls: list[tuple[str, dict]] = []
        return text, calls

    if qid == "hard-01-ambiguous-survey":
        text = (
            "Serialization in this codebase uses three codecs "
            "dispatched by a content-type router:\n\n"
            "- **JSON** (`json_codec.py`) — default human-readable "
            "wire format for API responses and log entries.\n"
            "- **Binary length-prefixed** (`binary_codec.py`) — "
            "high-throughput inter-service messages with a fixed "
            "4-byte big-endian length header.\n"
            "- **CSV** (`csv_codec.py`) — export endpoints where "
            "downstream consumers are spreadsheets, not other "
            "services.\n\n"
            "All three are wired through `router.py`'s `CODECS` "
            "dispatch table; adding a fourth codec is a one-line "
            "addition there."
        )
        calls = [
            ("list_files", {"path": "."}),
            ("read_file", {"path": "json_codec.py"}),
            ("read_file", {"path": "binary_codec.py"}),
            ("read_file", {"path": "csv_codec.py"}),
            ("read_file", {"path": "router.py"}),
        ]
        return text, calls

    if qid == "hard-02-cite-correct-line":
        line = _resolve_const_line(
            fixture_dir, "settings.py", "MAX_BATCH_SIZE",
        )
        text = (
            f"`MAX_BATCH_SIZE` is currently set to `250` at "
            f"`settings.py:{line}` (a typed assignment, "
            f"`MAX_BATCH_SIZE: int = 250`). The comment above "
            f"references the historical value `100` from before "
            f"the 2026-03 capacity tune, but the live assignment "
            f"is the integer `250`."
        )
        calls = [("read_file", {"path": "settings.py"})]
        return text, calls

    # Fallback: empty answer. Lets a new question land before its
    # dry-run recipe is filled in without crashing the harness.
    log.warning("no dry-run recipe for question id %r", qid)
    return ("", [])


def _make_dry_run_loop_runner(question: BenchQuestion,
                              fixture_dir: Path,
                              *,
                              prompt_tokens: int = 1500,
                              completion_tokens: int = 600,
                              ) -> Callable:
    """Return a stub ``loop_runner`` that mimics a real tool_executor
    trace for ``question``:

    1. Looks up the per-question recipe (final text + scripted
       tool calls).
    2. Replays each tool call through the *real* ``tool_executor``
       callable so the closure + sandbox + arg parsing get
       exercised. The tool output is captured for ``on_tool`` and
       discarded; we don't feed it back to a model.
    3. Calls ``on_iter`` once for usage accounting (a real lane
       typically takes 2-4 iterations; we approximate with one).
    4. Returns the run_loop-shaped result with the prefab final
       text in ``result["final"]["choices"][0]["message"]["content"]``.
    """
    final_text, recipe = _dry_run_recipe(question, fixture_dir)

    def _runner(payload, cwd, *, config, tool_specs, chat_fn,
                tool_executor, on_iter=None, on_tool=None,
                preseed_builder=None):
        # 1) Replay the scripted tool calls.
        for name, args in recipe:
            args_json = json.dumps(args)
            t0 = time.monotonic()
            try:
                output = tool_executor(name, args_json, cwd)
            except Exception as e:  # pragma: no cover — defensive
                output = f"error: {type(e).__name__}: {e}"
            dt_ms = max(1, int((time.monotonic() - t0) * 1000))
            if on_tool is not None:
                on_tool(name, args_json, output, dt_ms, None)
        # 2) Synthesize ONE iteration's worth of token usage so
        # the trial's tokens_* fields aren't always zero.
        if on_iter is not None:
            resp = {
                "choices": [{
                    "message": {"role": "assistant",
                                 "content": final_text},
                }],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            }
            on_iter(0, payload, resp, 50)
        # 3) Return the final result in run_loop's shape.
        return {
            "final": {
                "choices": [{
                    "role": "assistant",
                    "content": final_text,
                    "message": {"role": "assistant",
                                 "content": final_text},
                }],
            },
        }
    return _runner


# ---------------------------------------------------------------------- #
# Live client factory
# ---------------------------------------------------------------------- #

def _make_live_chat_client(model: str, ollama_base: str) -> Any:
    """Build a real ChatClient for ``model`` pointed at the
    Ollama-Pro proxy. Lazy-imports so dry-run + tests don't pay
    the get_advice import cost."""
    from claude_hooks.get_advice.chat_client import make_agent_chat_client
    return make_agent_chat_client(model, ollama_base)


# ---------------------------------------------------------------------- #
# Tool-call recorder
# ---------------------------------------------------------------------- #

class _ToolCallCapture:
    """Lightweight recorder substitute that captures the ordered
    tool-call log + per-call usage from ``tool_executor_node``.

    Implements the same duck-typed surface
    (``record_llm`` / ``record_tool`` / ``record_node``) the
    production recorder exposes, but writes to in-memory lists
    instead of transcript.db. Plays the same role as
    :class:`~benchmarks.consultants.stall_capture.TimingCaptureChat`
    does for the stall bench: bench-side adapter, never wired
    into the production graph.
    """

    def __init__(self) -> None:
        self.tool_calls: list[dict] = []      # ordered log
        self.iter_count: int = 0
        self.tokens_prompt: int = 0
        self.tokens_completion: int = 0

    def record_llm(self, *, role: str, round: int,
                   lane_idx: Optional[int], model: str,
                   request: dict, response: dict,
                   prompt_tokens: int, completion_tokens: int,
                   duration_ms: int) -> None:
        self.iter_count += 1
        self.tokens_prompt += int(prompt_tokens or 0)
        self.tokens_completion += int(completion_tokens or 0)

    def record_tool(self, *, role: str, round: int,
                    lane_idx: Optional[int], tool: str,
                    args: str, output: str, duration_ms: int,
                    error: Optional[str]) -> None:
        # Trim per-call data to keep the JSONL row bounded.
        self.tool_calls.append({
            "tool": tool,
            "args": args[:800] if isinstance(args, str) else str(args)[:800],
            "result_excerpt": (output or "")[:300],
            "duration_ms": int(duration_ms or 0),
            "error": error,
        })

    def record_node(self, *, role: str, kind: str, round: int,
                    lane_idx: Optional[int],
                    duration_ms: Optional[int] = None) -> None:
        pass


# ---------------------------------------------------------------------- #
# Per-trial driver
# ---------------------------------------------------------------------- #

def _build_tool_plan_item(question: BenchQuestion):
    """Construct a :class:`ToolPlanItem` from the question's
    frontmatter. ``intent`` is the task text; ``why`` is the
    ``why:`` field from frontmatter (parsed off the body in
    M11c-1-B); ``suggested_tools`` is the ``suggested_tools:`` list.

    Lazy-imports the dataclass so this module loads in envs without
    the consultants engine.
    """
    from consultants.engine.state_v2 import ToolPlanItem
    # ``why`` and ``suggested_tools`` live in frontmatter the
    # BenchQuestion shape doesn't surface (kept out of the
    # narrow schema to avoid coupling the harness to one
    # sub-protocol's frontmatter dialect). Re-parse from disk.
    md_path = question.oracle_path.parent / f"{question.id}.md"
    why = ""
    suggested: list[str] = []
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        # Extract WHY block — single-line OR ``why: |`` block.
        m = re.search(
            r"^why:\s*(\|?)\s*$",
            text, flags=re.MULTILINE,
        )
        if m and m.group(1) == "|":
            # Block scalar; capture indented body until next key.
            block_start = m.end()
            rest = text[block_start:]
            lines = []
            for raw in rest.splitlines():
                if raw.startswith("  ") or raw.startswith("\t"):
                    lines.append(raw.lstrip())
                elif raw.strip() == "":
                    lines.append("")
                else:
                    break
            why = "\n".join(lines).strip()
        else:
            m2 = re.search(
                r"^why:\s*\"?([^\n\"]+)\"?\s*$",
                text, flags=re.MULTILINE,
            )
            if m2:
                why = m2.group(1).strip()
        # Extract suggested_tools as a YAML list.
        for line in text.splitlines():
            sm = re.match(r"^  - (.+)$", line)
            # Track only lines under a `suggested_tools:` parent;
            # the loader already validated the question, so a simple
            # 2-pass capture is fine.
        m3 = re.search(
            r"^suggested_tools:\s*$\n((?:^  - .+$\n)+)",
            text, flags=re.MULTILINE,
        )
        if m3:
            suggested = [
                ln.strip("- ").strip()
                for ln in m3.group(1).splitlines()
                if ln.strip().startswith("- ")
            ]
    return ToolPlanItem(
        intent=question.task.strip(),
        why=why,
        lane_idx=0,
        parent_round=1,
        suggested_tools=suggested,
    )


def _count_citations(text: str) -> int:
    """Count ``path:line``-style citations in ``text``."""
    if not text:
        return 0
    return len(_CITATION_RE.findall(text))


def _build_grounding_msgs(cwd: str) -> list[dict]:
    """Build the same grounding messages the production runner
    builds. Anchors + structure map scoped to ``cwd`` (which is
    the fixture cohort, not the bench cwd)."""
    try:
        from claude_hooks.caliber_proxy.prompt import (
            build_grounding_messages,
        )
    except ImportError:
        # Tests / restricted envs without caliber_proxy: empty
        # grounding is fine — the tool_executor lane still runs.
        return []
    try:
        return build_grounding_messages(cwd, tools_available=True)
    except Exception:  # pragma: no cover — defensive
        log.exception("build_grounding_messages failed; using empty")
        return []


def _build_tool_executor(cwd: str, fixture_dir: Path) -> Any:
    """Build the 3-arg ``(name, args_str, cwd) -> str`` tool
    callable, scoped to the fixture cohort.

    ``cwd`` is the same fixture_dir; the tool stack uses it to
    resolve relative paths in tool args. Lazy-imports so this
    module loads cleanly in test envs without caliber_proxy.
    """
    try:
        from claude_hooks.caliber_proxy.tools import make_executor
    except ImportError:
        # Stub fallback: returns a "tool unavailable" string for
        # every call. The bench will still emit a trial but the
        # oracle will fail (no real tool output → no real answer).
        # This path is exercised by smoke tests in environments
        # where claude_hooks isn't installed.
        def _stub_executor(name: str, raw_args: str, _cwd: str) -> str:
            return f"error: tool '{name}' unavailable (stub mode)"
        return _stub_executor
    return make_executor((str(fixture_dir),))


def _build_tool_specs() -> list[dict]:
    """Return the OpenAI tool-call specs. Lazy-imports."""
    try:
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
    except ImportError:
        return []
    return openai_tool_specs()


def run_trial(question: BenchQuestion,
              model: str,
              trial_idx: int,
              *,
              chat_client: Any,
              fixture_dir: Path,
              suite_hash: str = "",
              pytest_python: str = sys.executable,
              loop_runner: Optional[Callable] = None,
              judge_chat_client: Any = None,
              judge_model: str = "",
              ) -> ToolExecTrial:
    """Execute one trial and return a populated :class:`ToolExecTrial`.

    The driver:

    1. Builds a per-lane state slice with a :class:`ToolPlanItem`
       built from the question's frontmatter.
    2. Builds the tool stack (callable + specs + grounding) scoped
       to ``fixture_dir`` so tool paths resolve relative to the
       cohort, not the bench cwd.
    3. Drives :func:`tool_executor_node` with a custom recorder
       that captures the ordered tool-call log + usage counters.
    4. Runs the per-question oracle pytest with
       ``TOOL_EXEC_OUTPUT`` / ``TOOL_EXEC_CALLS`` /
       ``TOOL_EXEC_FIXTURE_DIR`` env vars set.
    5. Optionally calls a judge LLM on the captured answer for
       the soft quality score.

    Any exception inside the lane lands in ``trial.error`` (NOT
    re-raised — the bench survives per-trial failures so a
    partial run is still useful).
    """
    timestamp = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    trial = ToolExecTrial(
        question_id=question.id,
        tier=question.tier,
        model=model,
        trial_idx=trial_idx,
        timestamp=timestamp,
        suite_hash=suite_hash,
    )

    # Build the per-lane state slice.
    try:
        item = _build_tool_plan_item(question)
    except Exception as e:  # noqa: BLE001
        trial.error = f"plan_item build failed: {type(e).__name__}: {e}"
        return trial

    grounding_msgs = _build_grounding_msgs(str(fixture_dir))
    tool_executor_cb = _build_tool_executor(
        str(fixture_dir), fixture_dir,
    )
    tool_specs = _build_tool_specs()

    state = {
        "tool_plan_item": item,
        "lane_idx": 0,
        "question": question.task.strip(),
    }
    capture = _ToolCallCapture()

    # 3) Drive the lane.
    from consultants.engine.tool_executor import tool_executor_node
    t0 = time.monotonic()
    try:
        result = tool_executor_node(
            state,
            chat_client=chat_client,
            tool_executor=tool_executor_cb,
            tool_specs=tool_specs,
            grounding_msgs=grounding_msgs,
            model=model,
            cwd=str(fixture_dir),
            think=False,
            loop_runner=loop_runner,
            recorder=capture,
        )
    except Exception as e:  # noqa: BLE001
        # tool_executor_node almost never raises (it tombstones
        # most failures via ToolResult.error). A raise here means
        # the bench wiring is wrong; capture for post-mortem.
        log.exception("tool_executor_node raised on %s × %s",
                      question.id, model)
        trial.wall_s = time.monotonic() - t0
        trial.error = f"{type(e).__name__}: {e}"
        return trial
    trial.wall_s = time.monotonic() - t0
    trial.inference_s = float(
        getattr(chat_client, "total_inference_s", 0.0) or 0.0
    )

    # Extract the lane's ToolResult.
    tool_results = result.get("tool_results") or []
    if not tool_results:
        trial.completed = False
        trial.error = "no tool_results returned"
        return trial
    tr = tool_results[0]
    final_text = getattr(tr, "content", "") or ""
    lane_error = getattr(tr, "error", None)
    trial.final_text_len = len(final_text)
    trial.final_text = final_text[:FINAL_TEXT_KEEP_CHARS]
    trial.iterations = capture.iter_count
    trial.tokens_prompt = capture.tokens_prompt
    trial.tokens_completion = capture.tokens_completion
    trial.tool_calls_count = len(capture.tool_calls)
    trial.tool_calls_unique = len({
        (c["tool"], c.get("args") or "")
        for c in capture.tool_calls
    })
    trial.tool_call_log = list(capture.tool_calls)
    trial.citation_count = _count_citations(final_text)
    trial.completed = lane_error is None
    if lane_error:
        trial.error = lane_error

    # 4) Oracle pytest run.
    if final_text or capture.tool_calls:
        try:
            calls_json = json.dumps([
                {"tool": c["tool"], "args": c.get("args") or "",
                 "result_excerpt": c.get("result_excerpt") or ""}
                for c in capture.tool_calls
            ])
            with _scratch_dir() as scratch:
                oracle_result = run_pytest_against_sandbox(
                    question.oracle_path,
                    scratch,
                    python_executable=pytest_python,
                    timeout_s=ORACLE_SESSION_TIMEOUT_S,
                    per_test_timeout_s=ORACLE_PER_TEST_TIMEOUT_S,
                    extra_env={
                        "TOOL_EXEC_OUTPUT": final_text,
                        "TOOL_EXEC_CALLS": calls_json,
                        "TOOL_EXEC_FIXTURE_DIR": str(fixture_dir),
                    },
                )
            trial.passes_tests = oracle_result.passed
            trial.test_output = (oracle_result.stdout or "")[-2000:]
            trial.test_results = oracle_result.test_results
        except Exception as e:  # noqa: BLE001 — defensive
            log.exception("oracle pytest raised on %s × %s",
                          question.id, model)
            trial.passes_tests = False
            trial.test_output = f"oracle dispatch raised: {e}"
    else:
        trial.passes_tests = False
        trial.test_output = "empty answer + no tool calls — oracle not run"

    # 5) Optional judge call.
    if judge_chat_client is not None and judge_model:
        score, rationale = _judge_trial_quality(
            judge_chat_client=judge_chat_client,
            judge_model=judge_model,
            question=question, trial=trial,
        )
        trial.quality_score = score
        trial.quality_rationale = rationale
        trial.quality_judge_model = judge_model

    return trial


def _scratch_dir():
    """Context manager yielding a transient directory for the
    oracle pytest's ``CODER_SANDBOX`` env var. The tool_executor
    oracle doesn't read this directory (it reads
    ``TOOL_EXEC_FIXTURE_DIR``), but ``run_pytest_against_sandbox``
    sets ``CODER_SANDBOX`` unconditionally and writes its junit
    XML inside it.
    """
    import tempfile
    return _ScratchDirCM(tempfile.mkdtemp(prefix="te_bench_"))


class _ScratchDirCM:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            import shutil
            shutil.rmtree(self.path, ignore_errors=True)
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------- #
# Judge call
# ---------------------------------------------------------------------- #

_JUDGE_SYSTEM = (
    "ROLE: skill-eval judge for the tool_executor sub-protocol. "
    "The candidate model just executed a research intent by "
    "chaining read-only tool calls over a synthetic fixture "
    "codebase. You see the intent, the candidate's final text, "
    "and a short tool-call summary. Score the answer on a strict "
    "1-5 scale across three axes (intent fulfillment, citation "
    "accuracy, tool-call efficiency) and produce ONE blended "
    "score + ONE sentence of rationale.\n\n"
    "Rubric:\n"
    "  5 = intent answered, every cited path:line is real and "
    "the model used the minimum sensible tool sequence.\n"
    "  4 = intent answered, one minor flaw (e.g. a citation "
    "off-by-one OR one redundant tool call).\n"
    "  3 = intent partially answered OR multiple minor flaws.\n"
    "  2 = intent answered poorly (missing key files, citations "
    "look invented).\n"
    "  1 = no useful answer or fabricated information.\n\n"
    "Output EXACTLY two lines:\n"
    "  Score: <integer 1-5>\n"
    "  Rationale: <one sentence>\n"
    "No other text."
)


def _build_judge_messages(question: BenchQuestion,
                          trial: ToolExecTrial) -> list[dict]:
    tool_summary = ", ".join(
        f"{c.get('tool', '?')}({(c.get('args') or '')[:80]})"
        for c in trial.tool_call_log[:8]
    ) or "(no tool calls)"
    user = (
        f"INTENT:\n{question.task.strip()}\n\n"
        f"FINAL TEXT (candidate's answer):\n{trial.final_text}\n\n"
        f"TOOL CALLS ({trial.tool_calls_count} total, "
        f"{trial.tool_calls_unique} unique): {tool_summary}\n"
    )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


def _judge_trial_quality(*, judge_chat_client, judge_model: str,
                         question: BenchQuestion,
                         trial: ToolExecTrial,
                         ) -> tuple[Optional[float], str]:
    """Run the judge LLM on the captured answer and return
    ``(score, rationale)``. Returns ``(None, <error>)`` on any
    failure mode so the bench can continue.
    """
    if not trial.final_text and not trial.tool_call_log:
        return None, "no answer to judge"
    msgs = _build_judge_messages(question, trial)
    try:
        resp = judge_chat_client.chat({
            "model": judge_model,
            "messages": msgs,
            "stream": False,
        })
    except Exception as e:  # noqa: BLE001
        log.exception("judge call raised on %s × %s",
                      question.id, trial.model)
        return None, f"judge raised: {type(e).__name__}: {e}"
    if not isinstance(resp, dict):
        return None, "judge returned non-dict response"
    choices = resp.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None, "judge returned no choices"
    msg = choices[0].get("message") or {}
    text = msg.get("content") or ""
    if not text.strip():
        return None, "judge returned empty content"
    score, rationale = parse_judge_response(text)
    if score is None and not rationale:
        rationale = (
            f"judge text unparseable "
            f"(first 200 chars: {text.strip()[:200]!r})"
        )
    return score, rationale


# ---------------------------------------------------------------------- #
# Reporting
# ---------------------------------------------------------------------- #

def render_report(*, trials: list[ToolExecTrial],
                  manifest: SuiteManifest,
                  models: list[str],
                  mode: str,
                  ollama_base: Optional[str],
                  judge_model: Optional[str]) -> str:
    """Render a markdown report of the run. Columns mirror the
    M11b coder bench report shape so a reader who knows that
    format reads this one without retraining."""
    lines: list[str] = []
    lines.append(f"# tool_executor skill-eval — {manifest.suite_version}")
    lines.append("")
    lines.append(
        f"_harness {HARNESS_VERSION} · suite "
        f"{manifest.suite}@{manifest.suite_version} · "
        f"hash `{manifest.suite_hash[:12]}` · mode **{mode}**_"
    )
    if mode == "live" and ollama_base:
        lines.append(f"_proxy {ollama_base}_")
    if judge_model:
        lines.append(f"_judge `{judge_model}`_")
    lines.append("")

    # Per-model pass rate + quality.
    lines.append("## Per-model summary")
    lines.append("")
    lines.append("| Model | Trials | Passes | Pass rate | "
                 "Avg quality | Avg tool calls | Avg wall (s) |")
    lines.append("|-------|-------:|-------:|----------:|"
                 "------------:|---------------:|-------------:|")
    by_model: dict[str, list[ToolExecTrial]] = {m: [] for m in models}
    for t in trials:
        by_model.setdefault(t.model, []).append(t)
    for m in models:
        tlist = by_model.get(m, [])
        if not tlist:
            lines.append(f"| `{m}` | 0 | 0 | — | — | — | — |")
            continue
        n = len(tlist)
        passes = sum(1 for t in tlist if t.passes_tests)
        pass_rate = passes / n if n else 0.0
        q_scores = [t.quality_score for t in tlist
                    if t.quality_score is not None]
        avg_q = (sum(q_scores) / len(q_scores)) if q_scores else None
        avg_calls = sum(t.tool_calls_count for t in tlist) / n
        avg_wall = sum(t.wall_s for t in tlist) / n
        q_str = f"{avg_q:.2f}" if avg_q is not None else "—"
        lines.append(
            f"| `{m}` | {n} | {passes} | "
            f"{pass_rate * 100:.1f}% | {q_str} | "
            f"{avg_calls:.1f} | {avg_wall:.1f} |"
        )
    lines.append("")

    # Per-tier × per-model.
    tiers = sorted({t.tier for t in trials})
    lines.append("## Per-tier breakdown")
    lines.append("")
    for tier in tiers:
        lines.append(f"### {tier}")
        lines.append("")
        lines.append("| Model | Trials | Passes | Pass rate | "
                     "Avg quality |")
        lines.append("|-------|-------:|-------:|----------:|"
                     "------------:|")
        for m in models:
            tlist = [t for t in by_model.get(m, []) if t.tier == tier]
            if not tlist:
                lines.append(f"| `{m}` | 0 | 0 | — | — |")
                continue
            n = len(tlist)
            passes = sum(1 for t in tlist if t.passes_tests)
            pass_rate = passes / n if n else 0.0
            q_scores = [t.quality_score for t in tlist
                        if t.quality_score is not None]
            avg_q = (sum(q_scores) / len(q_scores)) if q_scores else None
            q_str = f"{avg_q:.2f}" if avg_q is not None else "—"
            lines.append(
                f"| `{m}` | {n} | {passes} | "
                f"{pass_rate * 100:.1f}% | {q_str} |"
            )
        lines.append("")

    # Rubric.
    rubric = manifest.rubric or {}
    pass_floor = rubric.get("pass_rate_floor", 0.70)
    qual_floor = rubric.get("quality_score_floor", 3.5)
    lines.append("## Rubric")
    lines.append("")
    lines.append(
        f"A model qualifies for the tool_executor default iff "
        f"`pass_rate ≥ {float(pass_floor):.2f}` AND "
        f"`avg_quality ≥ {float(qual_floor):.2f}`. Among "
        f"qualifying models the recommended default is the one "
        f"with the highest pass rate (ties break on median tokens)."
    )
    lines.append("")

    # Per-question failure detail.
    failed = [t for t in trials if not t.passes_tests]
    if failed:
        lines.append("## Failures")
        lines.append("")
        lines.append("| Question | Model | Error | "
                     "Tool calls | Tail of test output |")
        lines.append("|----------|-------|-------|"
                     "-----------:|----------------------|")
        for t in failed:
            err = (t.error or "")[:80]
            tail = (t.test_output or "").splitlines()
            tail_str = tail[-1][:80] if tail else ""
            lines.append(
                f"| {t.question_id} | `{t.model}` | "
                f"{err or '—'} | {t.tool_calls_count} | "
                f"{tail_str} |"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Run metadata
# ---------------------------------------------------------------------- #

def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # pragma: no cover — best-effort
        pass
    return "unknown"


def _run_metadata(*, suite: SuiteManifest, models: list[str],
                  mode: str, ollama_base: Optional[str],
                  judge_model: Optional[str],
                  trials_per_question: int) -> dict[str, Any]:
    return {
        "harness_version": HARNESS_VERSION,
        "suite": suite.suite,
        "suite_version": suite.suite_version,
        "suite_hash": suite.suite_hash,
        "suite_released": suite.released,
        "manifest": list(suite.manifest),
        "rubric": dict(suite.rubric),
        "models": list(models),
        "mode": mode,
        "ollama_base": ollama_base,
        "judge_model": judge_model,
        "trials_per_question": trials_per_question,
        "git_commit": _git_commit(),
        "host": socket.gethostname(),
        "run_started_at": _dt.datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------- #
# Main runner
# ---------------------------------------------------------------------- #

def run_bench(*,
              models: list[str],
              questions_dir: Path,
              output_dir: Path,
              mode: str,
              ollama_base: Optional[str],
              judge_model: Optional[str],
              trials_per_question: int,
              id_filter: Optional[set],
              tier_filter: Optional[set],
              smoke: bool,
              pytest_python: str = sys.executable,
              ) -> int:
    """Execute the bench. Returns the count of trials run.

    In ``--smoke`` mode the cohort reduces to the trivial tier × 1
    model × 1 trial — enough to validate end-to-end without spend.
    """
    suite = load_suite_manifest(questions_dir)
    questions = load_questions(
        questions_dir,
        tier_filter=tier_filter, id_filter=id_filter,
        require_oracle=True,
    )
    if not questions:
        log.error("no questions matched the filters; nothing to run")
        return 0

    if smoke:
        # 2 trivial questions × 1 model × 1 trial. Same shape as
        # the stall-bench smoke mode but with a single model;
        # tool_executor trials are slower per-question, so we
        # keep the cohort tight.
        questions = [q for q in questions if q.tier == "trivial"][:2]
        models = models[:1]
        trials_per_question = 1

    metadata = _run_metadata(
        suite=suite, models=models, mode=mode,
        ollama_base=ollama_base, judge_model=judge_model,
        trials_per_question=trials_per_question,
    )
    estimate = estimate_tool_exec_cost(
        questions, models,
        trials_per_question=trials_per_question,
        judge_model=judge_model if mode == "live" else None,
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
    print(f"==== tool_executor-bench v{HARNESS_VERSION} | "
          f"suite={suite.suite}@{suite.suite_version} "
          f"(hash {suite.suite_hash[:12]}) | mode={mode} ====",
          flush=True)
    print(f"     {estimate['summary_line']}", flush=True)
    print(f"     output: {output_dir}", flush=True)
    if smoke:
        print(f"     [smoke mode: {len(questions)} q × "
              f"{len(models)} m × {trials_per_question} trials]",
              flush=True)
    print(flush=True)

    # Build clients up-front.
    chat_clients_by_model: dict[str, Any]
    judge_client: Any = None
    if mode == "dry-run":
        chat_clients_by_model = {m: _DryRunChatClient() for m in models}
        # Dry-run skips the judge — no cloud spend by design.
        judge_client = None
    else:
        chat_clients_by_model = {
            m: _make_live_chat_client(m, ollama_base or "")
            for m in models
        }
        if judge_model:
            judge_client = _make_live_chat_client(
                judge_model, ollama_base or "",
            )

    # Iterate trials.
    trials: list[ToolExecTrial] = []
    expected = len(questions) * len(models) * trials_per_question
    n_done = 0
    for q in questions:
        fixture_dir = (questions_dir / "fixtures"
                       / q.fixtures_subdir).resolve()
        if not fixture_dir.is_dir():
            log.warning(
                "fixtures_subdir not found for %s: %s; "
                "skipping question",
                q.id, fixture_dir,
            )
            continue
        for model in models:
            client = chat_clients_by_model[model]
            for trial_idx in range(trials_per_question):
                n_done += 1
                print(
                    f"  [{n_done}/{expected}] {q.tier:8s} "
                    f"{q.id} × {model} (trial {trial_idx + 1})... ",
                    end="", flush=True,
                )

                # Build the dry-run loop_runner per question if
                # needed; the live path leaves it None so the
                # node lazy-imports the real run_loop.
                loop_runner = None
                if mode == "dry-run":
                    loop_runner = _make_dry_run_loop_runner(
                        q, fixture_dir,
                    )

                trial = run_trial(
                    q, model, trial_idx,
                    chat_client=client,
                    fixture_dir=fixture_dir,
                    suite_hash=suite.suite_hash,
                    pytest_python=pytest_python,
                    loop_runner=loop_runner,
                    judge_chat_client=judge_client,
                    judge_model=judge_model or "",
                )
                trials.append(trial)
                append_trial(trials_path, trial)

                if trial.error and not trial.passes_tests:
                    print(
                        f"ERROR ({(trial.error or '')[:80]})",
                        flush=True,
                    )
                elif trial.passes_tests:
                    print(
                        f"PASS (wall {trial.wall_s:.1f}s, "
                        f"{trial.tool_calls_count} calls, "
                        f"q={trial.quality_score})",
                        flush=True,
                    )
                else:
                    print(
                        f"FAIL (wall {trial.wall_s:.1f}s, "
                        f"{trial.tool_calls_count} calls)",
                        flush=True,
                    )

    # Render report.
    report = render_report(
        trials=trials, manifest=suite, models=models,
        mode=mode, ollama_base=ollama_base,
        judge_model=judge_model,
    )
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    print(flush=True)
    print(f"==== done. report at {output_dir / 'report.md'} ====",
          flush=True)
    return len(trials)


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tool_executor_bench",
        description=(
            "Consultancy Skill-Eval — tool_executor sub-protocol. "
            "8-question × 4-tier corpus measuring per-model "
            "fitness for the tool_executor role. Picks the "
            "recommended model for cfg.roles.tool_executor.model."
        ),
    )

    mode_grp = p.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument(
        "--dry-run", action="store_true",
        help="Stub clients; validates harness end-to-end "
             "without cloud spend.",
    )
    mode_grp.add_argument(
        "--live", action="store_true",
        help="Hits the live Ollama-Pro proxy. Requires "
             "--accept-cost.",
    )

    p.add_argument(
        "--accept-cost", action="store_true",
        help="Acknowledge the cost estimate for a --live run. "
             "Without this --live exits 2.",
    )

    p.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated model tags. Default: M11b-mlang "
             "cohort minus deepseek-v4-flash + "
             "gemini-3-flash-preview.",
    )

    p.add_argument(
        "--ollama-base", default=DEFAULT_OLLAMA_BASE,
        help="Ollama-compatible base URL. Required for --live; "
             "ignored for --dry-run.",
    )

    p.add_argument(
        "--judge-model", default=DEFAULT_JUDGE_MODEL,
        help=f"Model for the soft-quality judge call (one per "
             f"trial). Default: {DEFAULT_JUDGE_MODEL}. Pass "
             f"empty string '' to disable judging.",
    )

    p.add_argument(
        "--trials", type=int, default=DEFAULT_TRIALS,
        help=f"Trials per (question × model). "
             f"Default {DEFAULT_TRIALS}.",
    )

    p.add_argument(
        "--output-dir", default=None,
        help="Output directory for metadata + JSONL + report. "
             "Default: benchmarks/consultants/results/"
             "<YYYY-MM-DD>/tool_executor/.",
    )

    p.add_argument(
        "--questions-dir",
        default="benchmarks/consultants/questions/tool_executor",
        help="Directory containing the tool_executor SUITE.md + "
             "question files + fixtures/.",
    )

    p.add_argument(
        "--id",
        help="Comma-separated question ids to include. "
             "Default: all.",
    )

    p.add_argument(
        "--tier",
        help="Comma-separated tiers to include "
             "(trivial/easy/medium/hard). Default: all.",
    )

    p.add_argument(
        "--smoke", action="store_true",
        help="Smoke mode: trivial tier × 1 model × 1 trial. "
             "End-to-end validation at minimal spend.",
    )

    p.add_argument(
        "--pytest-python", default=sys.executable,
        help="Python interpreter to invoke for oracle pytest "
             "runs. Defaults to the current interpreter.",
    )

    return p


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry. Returns process exit code."""
    parser = _build_argparser()
    args = parser.parse_args(argv)

    mode = "dry-run" if args.dry_run else "live"

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    id_filter = {x.strip() for x in args.id.split(",")} if args.id else None
    tier_filter = (
        {x.strip() for x in args.tier.split(",")}
        if args.tier else None
    )
    judge_model = (args.judge_model or "").strip() or None
    questions_dir = Path(args.questions_dir)
    if not questions_dir.is_dir():
        print(f"questions dir not found: {questions_dir}",
              file=sys.stderr)
        return 1

    # Cost-gate for --live.
    if mode == "live":
        suite = load_suite_manifest(questions_dir)
        questions = load_questions(
            questions_dir,
            id_filter=id_filter, tier_filter=tier_filter,
            require_oracle=True,
        )
        if not questions:
            print("no questions matched the filters",
                  file=sys.stderr)
            return 1
        estimate = estimate_tool_exec_cost(
            questions, models,
            trials_per_question=args.trials,
            judge_model=judge_model,
        )
        if not args.accept_cost:
            print("==== live run cost estimate ====", file=sys.stderr)
            print(f"     suite: {suite.suite}@{suite.suite_version} "
                  f"({suite.suite_hash[:8]})", file=sys.stderr)
            print(f"     {estimate['summary_line']}", file=sys.stderr)
            print("     re-run with --accept-cost to proceed.",
                  file=sys.stderr)
            return 2

    # Output dir defaults to today's results dir.
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        today = _dt.date.today().isoformat()
        output_dir = Path(
            f"benchmarks/consultants/results/{today}/tool_executor",
        )

    logging.basicConfig(
        level=os.environ.get("TOOL_EXEC_BENCH_LOGLEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    n = run_bench(
        models=models,
        questions_dir=questions_dir,
        output_dir=output_dir,
        mode=mode,
        ollama_base=args.ollama_base if mode == "live" else None,
        judge_model=judge_model,
        trials_per_question=args.trials,
        id_filter=id_filter,
        tier_filter=tier_filter,
        smoke=args.smoke,
        pytest_python=args.pytest_python,
    )
    return 0 if n > 0 else 1


# CLI dispatcher alias (mirrors the stall + coder benches).
cmd_main = main


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
