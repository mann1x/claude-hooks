"""Shared infrastructure for the M11 benchmark suite.

Three responsibilities:

1. **Question loading** — markdown files with YAML-ish frontmatter
   live under ``benchmarks/consultants/questions/<bench>/``. Frontmatter
   declares id / tier / source / task / sandbox_path / oracle / notes.
   The loader returns a typed ``BenchQuestion`` per file and lets the
   runner filter by tier or id.

2. **Trial result schema** — ``CoderTrial`` (M11b) records every
   measurable metric the bench computes per (question × model) trial.
   Pydantic-free dataclass with a ``to_dict()`` for JSON dump; the
   harness writes one trial per row as soon as it completes so a
   Ctrl-C mid-run preserves the prior trials.

3. **Cost estimator** — token-budget summary surfaced BEFORE a live
   run starts. Per the 2026-05-16 design discussion: this is
   informational (you opted in via --accept-cost) not a hard gate,
   so it's a single summary line. The estimator pulls per-model
   per-token coefficients from internal heuristics tuned against the
   actual 192.168.178.2:11433 cloud-Ollama trace from the M0-M9
   sessions.

Plus a few subprocess helpers for the M11b coder bench specifically:
``run_pytest_against_sandbox`` (does the model's code pass tests?)
and ``measure_complexity`` (radon-based cyclomatic complexity,
soft-deps so the bench runs even without radon installed).

Pure-Python; importable in the main ``claude-hooks`` env. Live runs
need the consultants env (langgraph + claude_hooks.agent_loop on the
path) — same as the rest of the v2 stack.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("benchmarks.consultants.harness")

# Bumped when the trial schema or core API contract changes in a
# non-backwards-compat way. Old results JSON with a lower version
# is readable (additive-only changes); the renderer warns when
# mixing versions.
HARNESS_VERSION = "1.0"


# ============================================================== #
# Question schema
# ============================================================== #

# Tiers are advisory — the runner reports per-tier breakdowns but
# doesn't gate on them. Order matters for tier-filter parsing and
# for the markdown report layout.
TIERS: tuple[str, ...] = ("trivial", "easy", "medium", "hard")


@dataclass(frozen=True)
class BenchQuestion:
    """One bench item loaded from disk. ``oracle_path`` is the
    absolute path to the pytest file that verifies the produced
    code; the harness sets ``CODER_SANDBOX`` env var before
    invoking pytest so the oracle imports relative to the
    coder's per-trial sandbox dir.
    """
    id: str
    tier: str
    source: str               # e.g. "humaneval/23"
    task: str                 # natural-language instruction the coder sees
    sandbox_path: str         # required filename, e.g. "truncate.py"
    oracle_path: Path
    notes: str = ""
    body: str = ""            # the markdown body after the frontmatter


# Lazy frontmatter parser — accepts the common pattern
#   ---
#   key: value
#   multi: |
#     line one
#     line two
#   ---
# without pulling PyYAML as a hard dep. Limited but covers our
# schema; we never need YAML inline lists / nested maps for the
# bench questions.
_FRONTMATTER_RE = re.compile(
    r"^---\n(.*?)\n---\n(.*)$", re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, remaining_body). On no-match,
    returns ({}, text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    return _parse_simple_yaml(fm_text), body


def _strip_quotes(s: str) -> str:
    """Strip a single layer of surrounding ASCII quotes."""
    if (s.startswith('"') and s.endswith('"')) \
            or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _parse_simple_yaml(text: str) -> dict:
    """Subset YAML supporting:

    - ``key: value`` (one per line)
    - ``key: |`` block scalars (indented continuation lines)
    - ``key:`` followed by indented ``- item`` lines → list[str]
    - ``key:`` followed by indented ``subkey: value`` lines → dict
    - ``#`` line comments (full-line only)

    Inline list / map syntax (``[a, b]``, ``{k: v}``) is NOT
    supported on purpose — keeping the surface small avoids a
    PyYAML dep and a bench-question can be re-written if it tries
    something fancier.
    """
    out: dict = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        # Top-level key must start at column 0.
        if line[:1] in (" ", "\t"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "|":
            # Block scalar — consume indented lines.
            i += 1
            block: list[str] = []
            while i < len(lines):
                lookahead = lines[i]
                if lookahead.strip() == "":
                    block.append("")
                    i += 1
                    continue
                if not lookahead.startswith(("  ", "\t")):
                    break
                block.append(lookahead.lstrip())
                i += 1
            out[key] = "\n".join(block).rstrip()
            continue
        if value == "":
            # Bare key — collect indented children (list of '- item'
            # OR map of 'subkey: subvalue'). Peek at the first
            # non-blank indented line to decide which.
            i += 1
            children: list[str] = []
            while i < len(lines):
                lookahead = lines[i]
                if lookahead.strip() == "":
                    i += 1
                    continue
                if not lookahead.startswith(("  ", "\t")):
                    break
                children.append(lookahead)
                i += 1
            if not children:
                out[key] = ""
                continue
            # Decide shape by inspecting the first child.
            first = children[0].lstrip()
            if first.startswith("- "):
                # List of strings.
                items: list[str] = []
                for c in children:
                    s = c.lstrip()
                    if s.startswith("- "):
                        items.append(_strip_quotes(s[2:].strip()))
                out[key] = items
            else:
                # Nested map. Re-parse with dedent.
                # We must compute the indent of the first child line
                # then strip exactly that many leading spaces from
                # each child before recursing.
                indent = len(children[0]) - len(children[0].lstrip())
                dedented = "\n".join(
                    c[indent:] if len(c) >= indent else c
                    for c in children
                )
                out[key] = _parse_simple_yaml(dedented)
            continue
        # Inline ``key: value`` — strip wrapping quotes if any.
        out[key] = _strip_quotes(value)
        i += 1
    return out


def load_questions(
    directory: Path,
    *,
    tier_filter: Optional[Iterable[str]] = None,
    id_filter: Optional[Iterable[str]] = None,
) -> list[BenchQuestion]:
    """Load every ``*.md`` under ``directory`` whose name does NOT
    end with ``-oracle.py`` (oracles are .py files alongside the
    markdown). Returns sorted by id for reproducibility.

    ``tier_filter`` — keep only questions whose ``tier`` is in
    the set; None means all tiers.
    ``id_filter`` — keep only questions matching one of the ids;
    None means all ids.
    """
    if not directory.is_dir():
        raise FileNotFoundError(
            f"bench questions directory not found: {directory}"
        )
    tier_set = set(tier_filter) if tier_filter else None
    id_set = set(id_filter) if id_filter else None
    out: list[BenchQuestion] = []
    for md_path in sorted(directory.glob("*.md")):
        # SUITE.md is the manifest, not a question; load_suite_manifest
        # handles it separately. README.md / NOTES.md follow the same
        # convention.
        if md_path.name in ("SUITE.md", "README.md", "NOTES.md"):
            continue
        text = md_path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        if not fm:
            log.warning(
                "skipping %s: no frontmatter", md_path,
            )
            continue
        qid = fm.get("id") or md_path.stem
        tier = fm.get("tier") or "unknown"
        if tier_set is not None and tier not in tier_set:
            continue
        if id_set is not None and qid not in id_set:
            continue
        oracle_filename = fm.get("oracle")
        if not oracle_filename:
            log.warning(
                "skipping %s: no oracle frontmatter field", md_path,
            )
            continue
        oracle_path = directory / oracle_filename
        if not oracle_path.is_file():
            log.warning(
                "skipping %s: oracle file %s not found",
                md_path, oracle_path,
            )
            continue
        out.append(BenchQuestion(
            id=qid, tier=tier,
            source=fm.get("source") or "",
            task=fm.get("task") or "",
            sandbox_path=fm.get("sandbox_path") or "",
            oracle_path=oracle_path,
            notes=fm.get("notes") or "",
            body=body.strip(),
        ))
    return out


# ============================================================== #
# Suite manifest (SUITE.md) — versioning + reproducibility
# ============================================================== #

@dataclass(frozen=True)
class SuiteManifest:
    """Parsed SUITE.md. ``suite_hash`` is computed from the
    canonical sorted manifest + every question file's content
    hash, so a non-content-changing whitespace edit doesn't flap
    the recorded baseline. The renderer surfaces it on every
    results report so a reader can verify which suite version +
    content the numbers came from.
    """
    suite: str                  # "coder" | "stall" | "tool_executor"
    suite_version: str          # "1.0", "1.1", ...
    released: str               # ISO date the suite version landed
    manifest: tuple[str, ...]   # ordered question ids
    rubric: dict                # rubric thresholds (suite-specific)
    suite_hash: str             # sha256(manifest + question file contents)


def _hash_for_suite(directory: Path, manifest: list[str]) -> str:
    """Stable hash over (sorted manifest ids + each question's
    markdown bytes + oracle bytes). Used to detect undeclared
    drift — if someone edited a question without bumping
    suite_version, the hash changes and the report flags it.
    """
    h = hashlib.sha256()
    for qid in sorted(manifest):
        h.update(qid.encode("utf-8"))
        h.update(b"\x00")
        md = directory / f"{qid}.md"
        if md.is_file():
            h.update(md.read_bytes())
        oracle = directory / f"{qid}-oracle.py"
        if oracle.is_file():
            h.update(oracle.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


def load_suite_manifest(directory: Path) -> SuiteManifest:
    """Read SUITE.md from ``directory``, parse the frontmatter,
    and compute the content hash. Raises FileNotFoundError when
    SUITE.md is missing.
    """
    suite_md = directory / "SUITE.md"
    if not suite_md.is_file():
        raise FileNotFoundError(
            f"suite manifest not found: {suite_md}"
        )
    fm, _ = _parse_frontmatter(suite_md.read_text(encoding="utf-8"))
    if not fm:
        raise ValueError(f"SUITE.md has no frontmatter: {suite_md}")
    # ``manifest:`` is a list in proper YAML; the simple parser
    # returns a Python list[str] directly. Tolerate the legacy
    # string-with-newlines shape too in case someone hand-edits.
    raw_manifest = fm.get("manifest", [])
    manifest_ids: list[str] = []
    if isinstance(raw_manifest, list):
        manifest_ids = [str(x).strip() for x in raw_manifest if x]
    elif isinstance(raw_manifest, str):
        for line in raw_manifest.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("-"):
                line = line[1:].strip()
            manifest_ids.append(line)
    # rubric: parsed as a dict by the simple parser; coerce numeric
    # values where the canonical rubric expects floats.
    raw_rubric = fm.get("rubric", {})
    rubric: dict = {}
    if isinstance(raw_rubric, dict):
        for k, v in raw_rubric.items():
            try:
                rubric[k] = float(v)
            except (TypeError, ValueError):
                rubric[k] = v
    return SuiteManifest(
        suite=fm.get("suite") or directory.name,
        suite_version=str(fm.get("suite_version") or "0.0"),
        released=fm.get("released") or "",
        manifest=tuple(manifest_ids),
        rubric=rubric,
        suite_hash=_hash_for_suite(directory, manifest_ids),
    )


# ============================================================== #
# Trial result schema (M11b)
# ============================================================== #

@dataclass
class CoderTrial:
    """One (question × model) trial result.

    Two-axis correctness signals (v1.0.1+):
      ``passes_algorithm`` — all algorithmic tests pass. Decides
        the cohort pass-rate column in the report. This is the
        primary "does the code work?" signal.
      ``passes_constraints`` — all constraint-tagged tests pass.
        Measures instruction-following (did the model use the
        required function names, output formats, banned-import
        rules?). Independent of algorithmic correctness.
      ``passes_tests`` — back-compat alias = ``passes_algorithm``
        in v1.0.1+. In v1.0 this meant "all tests passed under
        ``pytest -x``", which conflated the two axes.
      ``test_results`` — per-test detail from the pytest junit
        XML: ``{test_name: {"status": "passed"|"failed"|"error"
        |"skipped", "msg": str}}``. Empty when the trial didn't
        compile (pytest never ran).
      ``constraint_violations`` — convenience: the test names
        marked ``@pytest.mark.constraint`` that did NOT pass.
        First-class field so the report renderer can surface
        them without re-parsing test_results.

    ``quality_score`` is the LLM-judge soft signal (1-5, None
    when judge skipped or trial didn't compile);
    ``code_lines`` / ``complexity`` / ``tokens_*`` / ``wall_s`` /
    ``iterations`` measure cost.

    ``sandbox_dir`` records where the produced files live (under
    ``benchmarks/consultants/results/<date>/coder/<trial-id>/``) so
    a post-hoc audit can re-read the actual code the model emitted.
    """
    question_id: str
    tier: str
    model: str
    # Hard-correctness signals
    compiles: bool = False
    passes_tests: bool = False               # alias for passes_algorithm (v1.0.1+)
    passes_algorithm: bool = False           # v1.0.1: algorithmic tests
    passes_constraints: bool = False         # v1.0.1: constraint tests
    test_output: str = ""
    test_results: dict = field(default_factory=dict)  # v1.0.1: per-test detail
    constraint_violations: list = field(default_factory=list)  # v1.0.1
    # Cost signals
    wall_s: float = 0.0                       # total trial wall (incl. retries)
    inference_s: float = 0.0                  # v1.0.1: cumulative SUCCESSFUL-attempt inference
                                              # time only (excludes failed-attempt timeouts +
                                              # exponential-backoff sleeps). Use this for
                                              # cost-comparison rankings; wall_s stays for
                                              # the operator-facing "wall clock" number.
    iterations: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    code_lines: int = 0
    complexity: Optional[int] = None
    # Soft-quality signal (LLM judge) — pure static read of the
    # source. Cannot tell if the code WORKS, only if it READS WELL.
    quality_score: Optional[float] = None
    quality_rationale: str = ""
    # v1.0.1: **audit judge** — a second judge that fires on EVERY
    # compiled trial, using one of two prompts depending on the
    # algorithm-axis outcome:
    #
    #   mode="post_test"  (passes_algorithm=False)
    #       Sees the failing test name + first 200 chars of failure
    #       msg. Rates 1-5 on EDGE-CASE-AWARENESS: should the author
    #       have anticipated this failure?
    #
    #   mode="robustness" (passes_algorithm=True)
    #       Sees only the task + code. Rates 1-5 on ROBUSTNESS: name
    #       ONE plausible edge case the visible tests don't cover.
    #       Captures latent bugs in passing code (e.g. a sort that
    #       happens to work despite a buggy pivot).
    #
    #   mode="skipped"
    #       No audit judge configured, or the trial errored / didn't
    #       compile.
    #
    # The divergence between ``quality_score`` (read-only / first
    # judge) and ``quality_audit_score`` (this field, from a
    # DIFFERENT model) gives cross-judge cross-model robustness on
    # every trial. A 5 -> 2 drop in post_test mode is the
    # "camouflaged bug" signal; a 5 -> 2 drop in robustness mode is
    # the "latent edge case in passing code" signal.
    quality_audit_score: Optional[float] = None
    quality_audit_rationale: str = ""
    quality_audit_judge_model: str = ""    # which model judged
    quality_audit_target_test: str = ""    # post_test mode: which
                                           # failure was shown.
                                           # robustness mode: "".
    quality_audit_mode: str = "skipped"    # one of: "post_test" /
                                           # "robustness" / "skipped"
    # Bookkeeping
    sandbox_dir: str = ""
    timestamp: str = ""        # ISO-8601 UTC; set at trial start
    error: Optional[str] = None
    # The files the coder wrote: list of {"path": str, "bytes": int}
    files_written: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================== #
# Per-model cost coefficients
# ============================================================== #

# Heuristic coefficients per model — derived from the
# csl-2026-05-* trace battery's measured token-per-iteration
# distributions for code-gen / write-heavy tasks. These are
# upper-mid estimates so the summary line says "around X" without
# being scarily wrong on the low side.
#
# Schema: model -> (avg_iterations, avg_prompt_tokens_per_iter,
#                   avg_completion_tokens_per_iter)
#
# Unknown models default to a conservative 5-iter / 8000-prompt /
# 1500-completion mid-point so callers can ship new models without
# updating this table first.
_COST_COEFFS: dict[str, tuple[int, int, int]] = {
    "kimi-k2.6:cloud":               (5, 8000, 1500),
    "qwen3-next:cloud":              (5, 8000, 1500),
    "glm-5.1:cloud":                 (5, 8000, 1400),
    "gemma4:31b-cloud":              (4, 6500, 1200),
    "deepseek-v4-pro:cloud":         (6, 9000, 1800),
    "gemini-3-flash-preview:cloud":  (6, 9000, 2000),
    "nemotron-3-super:cloud":        (5, 8000, 1500),
    "kimi-k2:cloud":                 (5, 8000, 1500),
}


def _coeffs_for(model: str) -> tuple[int, int, int]:
    return _COST_COEFFS.get(model, (5, 8000, 1500))


def estimate_cost(questions: list[BenchQuestion],
                  models: list[str],
                  *,
                  judge_model: Optional[str] = None) -> dict[str, Any]:
    """Token-budget estimate for a coder-bench run.

    Returns a dict with:

    - ``trials`` — total trial count (len(questions) * len(models))
    - ``by_model`` — per-model {iterations, prompt_tokens,
      completion_tokens, total_tokens}
    - ``total_tokens`` — grand total across all models + judge
    - ``judge_tokens`` — separate judge-LLM cost line (one judge
      call per trial that compiled)
    - ``summary_line`` — one-line string for the run banner

    The estimate is intentionally a rough single number — refine
    only when the per-run trace diverges from these coefficients
    by more than ~30%.
    """
    n_q = len(questions)
    by_model: dict[str, dict[str, int]] = {}
    total = 0
    for m in models:
        iters, pt_per, ct_per = _coeffs_for(m)
        prompt = iters * pt_per * n_q
        completion = iters * ct_per * n_q
        sub_total = prompt + completion
        by_model[m] = {
            "iterations": iters * n_q,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": sub_total,
        }
        total += sub_total
    # Judge: one call per (question × model) that compiles, ~1500
    # prompt tokens (problem + code + rubric) + ~150 completion (a
    # score + a sentence). Assume 80% compile rate for the rough
    # estimate (the real rate per model is in the report).
    judge_tokens = 0
    if judge_model:
        n_compiles = int(0.8 * n_q * len(models))
        judge_iters, judge_pt, judge_ct = _coeffs_for(judge_model)
        # Override per-iteration sizes for the judge — it's a
        # single call, not an agent loop.
        judge_tokens = n_compiles * (1500 + 150)
        total += judge_tokens
    summary_line = (
        f"{len(models)} models × {n_q} questions = "
        f"{len(models) * n_q} trials. "
        f"~{total / 1_000_000:.2f}M tokens estimated"
        + (
            f" (+~{judge_tokens / 1_000:.0f}K for the {judge_model} judge)"
            if judge_model else ""
        )
        + "."
    )
    return {
        "trials": len(models) * n_q,
        "by_model": by_model,
        "judge_model": judge_model,
        "judge_tokens": judge_tokens,
        "total_tokens": total,
        "summary_line": summary_line,
    }


# ============================================================== #
# Oracle grader (M11b)
# ============================================================== #

@dataclass
class OracleResult:
    """Outcome of running the oracle pytest file against the
    produced sandbox.

    v1.0.1: ``passed`` keeps the back-compat semantic ("all tests
    pass"), but per-test data lives in ``test_results`` so the
    bench can split algorithm vs constraint at trial-finalize
    time. The pytest command line dropped ``-x`` so EVERY test
    runs to completion regardless of earlier failures — this is
    what makes the algorithm/constraint split possible.
    """
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    test_results: dict = field(default_factory=dict)


def _parse_junit_xml(junit_path: Path) -> dict:
    """Parse a pytest ``--junitxml`` file into a
    ``{test_name: {"status": ..., "msg": ...}}`` dict.

    Status values: ``"passed"``, ``"failed"``, ``"error"``,
    ``"skipped"``. Missing/unparseable file yields an empty dict
    — the bench then treats the trial as "pytest never ran",
    which matches how a no-compile or timeout looks anyway.

    Messages are truncated to a defensive ceiling (2 KB each) so
    a single chatty assertion can't bloat the trials.jsonl row.
    """
    if not junit_path.is_file():
        return {}
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(junit_path))
    except Exception:  # pragma: no cover — corrupt junit XML
        return {}
    root = tree.getroot()
    out: dict = {}
    # ``root`` may be ``testsuites`` (multiple) or a single
    # ``testsuite``; iter both shapes.
    for tc in root.iter("testcase"):
        name = tc.get("name") or "(unnamed)"
        fail = tc.find("failure")
        err = tc.find("error")
        skip = tc.find("skipped")
        if fail is not None:
            msg = ((fail.get("message") or "") + "\n"
                   + (fail.text or ""))[:2000]
            out[name] = {"status": "failed", "msg": msg.strip()}
        elif err is not None:
            msg = ((err.get("message") or "") + "\n"
                   + (err.text or ""))[:2000]
            out[name] = {"status": "error", "msg": msg.strip()}
        elif skip is not None:
            msg = (skip.get("message") or skip.text or "")[:2000]
            out[name] = {"status": "skipped", "msg": msg.strip()}
        else:
            out[name] = {"status": "passed", "msg": ""}
    return out


def parse_constraint_tests(oracle_path: Path) -> set:
    """Return the set of ``test_<name>`` function names in the
    given oracle file that carry the ``@pytest.mark.constraint``
    decorator immediately above them.

    Tolerant of:
      - Multiple decorators stacked (the constraint mark only
        needs to be one of them).
      - Blank lines between decorator and ``def``.
      - Method-style oracles (rare; we still match the def line).
    The check is **lexical** — no import needed, no pytest
    collection. Faster than re-running pytest with
    ``--collect-only`` and lets the bench inspect the oracle
    even when subprocess pytest isn't running.
    """
    if not oracle_path.is_file():
        return set()
    text = oracle_path.read_text(encoding="utf-8", errors="replace")
    out: set = set()
    pending = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "@pytest.mark.constraint":
            pending = True
            continue
        if line.startswith("@"):
            # Other decorators don't reset; they stack.
            continue
        if line.startswith("def test_"):
            if pending:
                # Extract the function name.
                name = line[4:].split("(", 1)[0].strip()
                if name:
                    out.add(name)
            pending = False
            continue
        if line == "" or line.startswith("#"):
            # Blank line / comment between decorator and def is fine.
            continue
        # Any other code line breaks the pending association.
        pending = False
    return out


def run_pytest_against_sandbox(oracle_path: Path,
                               sandbox_dir: Path,
                               *,
                               python_executable: str = sys.executable,
                               timeout_s: float = 120.0,
                               per_test_timeout_s: float = 15.0) -> OracleResult:
    """Run the oracle pytest file with ``CODER_SANDBOX`` env var
    pointing at the produced code's directory. Returns OracleResult.

    v1.0.1 changes vs v1.0:
      - ``-x`` (exit on first failure) dropped — every test now
        runs to completion.
      - ``--junitxml=`` writes per-test results.
      - ``--timeout=<per_test_timeout_s>`` via ``pytest-timeout``
        plugin (with ``--timeout-method=signal``): individual hung
        tests are killed without aborting the whole pytest session,
        and the junit XML records the failure with the surviving
        per-test data. Prior subprocess-only timeout SIGKILLed the
        whole pytest process before junit XML write, leaving the
        bench with zero per-test data for any runaway-recursion
        trial. The 2026-05-17 quicksort × flash trial exposed this:
        flash had an infinite recursion, the subprocess hit the
        60s wall, and the audit judge fell back to ``robustness``
        mode (correctly, given missing data) but we lost the
        post_test signal entirely.

    The oracle file is responsible for importing relative to
    ``$CODER_SANDBOX``.

    Two timeouts:
      ``per_test_timeout_s`` (default 15s) — each test gets this
        long. Honest tests finish in <1s; this catches runaway
        recursion / busy-loop bugs while letting legitimate edge-
        case tests (large random arrays, etc.) complete.
      ``timeout_s`` (default 120s) — total session safety net.
        Should never fire under normal pytest-timeout operation
        but kept as a defense against pytest-timeout plugin
        edge cases (segfault, fork bomb, etc.).
    """
    import os
    t0 = time.monotonic()
    env = dict(os.environ)
    env["CODER_SANDBOX"] = str(sandbox_dir)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    junit_path = sandbox_dir / "_pytest_junit.xml"
    try:
        junit_path.unlink()
    except FileNotFoundError:
        pass
    try:
        proc = subprocess.run(
            [python_executable, "-m", "pytest", str(oracle_path),
             "-q", "--no-header",
             "-p", "no:cacheprovider",
             f"--junitxml={junit_path}",
             # Per-test timeout. ``signal`` method uses SIGALRM
             # which pytest's pytest-timeout plugin catches and
             # records as a test failure in the junit XML — the
             # rest of the test session keeps running. POSIX-only
             # but the bench is POSIX-only anyway.
             f"--timeout={per_test_timeout_s}",
             "--timeout-method=signal"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        # Outer safety net fired — pytest-timeout's per-test logic
        # didn't fire fast enough. Still parse whatever junit got
        # written before the SIGKILL.
        return OracleResult(
            passed=False, returncode=-1,
            stdout=(e.stdout or b"").decode("utf-8", errors="replace")
                   if isinstance(e.stdout, (bytes, bytearray))
                   else (e.stdout or ""),
            stderr=f"(pytest session timed out after {timeout_s}s)",
            duration_s=time.monotonic() - t0,
            test_results=_parse_junit_xml(junit_path),
        )
    return OracleResult(
        passed=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_s=time.monotonic() - t0,
        test_results=_parse_junit_xml(junit_path),
    )


# ============================================================== #
# Code-quality measurements (M11b)
# ============================================================== #

def count_code_lines(file_path: Path) -> int:
    """Non-empty, non-comment lines in a Python file. Defensive
    against missing files / decode errors — returns 0 instead of
    raising so a tombstone trial still has a number to report.
    """
    if not file_path.is_file():
        return 0
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count += 1
    return count


def measure_complexity(file_path: Path) -> Optional[int]:
    """Cyclomatic complexity via ``radon`` if installed, else None.

    Returns the maximum complexity across all functions in the file
    — a single number per trial is enough signal for the bench
    summary; per-function granularity is in the trial's
    ``files_written`` for post-hoc analysis.
    """
    if not file_path.is_file():
        return None
    try:
        from radon.complexity import cc_visit  # type: ignore
    except ImportError:
        return None
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        results = cc_visit(text)
    except Exception:  # pragma: no cover — radon parse error
        log.exception("radon failed on %s", file_path)
        return None
    if not results:
        return 0
    return max(int(r.complexity) for r in results)


# ============================================================== #
# Judge LLM helper (M11b — code-quality scoring)
# ============================================================== #

# Mapping from sandbox-file extension to (display_name, fence_tag).
# Drives the language-aware judge prompt — the 2026-05-16 coder_mlang
# full run abort caught a bug where every submission was wrapped in a
# ```python fence and the judge was told "you are a senior Python code
# reviewer", so all 10 C trials returned quality=1.0 regardless of
# whether the C source passed the oracle.
_JUDGE_LANG_BY_EXT: dict[str, tuple[str, str]] = {
    ".py":   ("Python", "python"),
    ".rs":   ("Rust",   "rust"),
    ".go":   ("Go",     "go"),
    ".c":    ("C",      "c"),
    ".h":    ("C",      "c"),
    ".cpp":  ("C++",    "cpp"),
    ".cc":   ("C++",    "cpp"),
    ".cxx":  ("C++",    "cpp"),
    ".hpp":  ("C++",    "cpp"),
    ".cs":   ("C#",     "csharp"),
    ".java": ("Java",   "java"),
    ".ts":   ("TypeScript", "typescript"),
    ".js":   ("JavaScript", "javascript"),
}


def judge_lang_for_path(sandbox_path: str) -> tuple[str, str]:
    """Return ``(display_name, fence_tag)`` for a sandbox file path.
    Falls back to ``("Python", "python")`` for unknown extensions so
    pre-existing Python-only callers stay on the original behavior."""
    if not sandbox_path:
        return ("Python", "python")
    suffix = ""
    # Tolerate plain filenames (no Path import to keep the helper
    # zero-cost for the dry-run hot path).
    for i in range(len(sandbox_path) - 1, -1, -1):
        if sandbox_path[i] == ".":
            suffix = sandbox_path[i:].lower()
            break
        if sandbox_path[i] in "/\\":
            break
    return _JUDGE_LANG_BY_EXT.get(suffix, ("Python", "python"))


def build_judge_system(language: str = "Python") -> str:
    """Build a language-aware judge-system prompt. ``language`` is
    interpolated into the reviewer persona + rubric so a Rust judge
    rates Rust idioms (not Python idioms) and a C judge does not
    treat manual indexing as a code smell."""
    return (
        f"You are a senior {language} code reviewer. You are given a "
        "code-generation task and the code a junior engineer produced. "
        "Score the code on a single integer scale 1-5 with this rubric:\n\n"
        "1 — Broken. Doesn't solve the task or has obvious bugs.\n"
        "2 — Solves the basic case but misses obvious edge cases or "
        "uses confusing structure.\n"
        f"3 — Correct for the spec, but overly verbose / non-idiomatic / "
        f"missing simple {language} idioms.\n"
        "4 — Correct and idiomatic. Reasonable structure, edge cases "
        "considered.\n"
        "5 — Excellent. Minimal, idiomatic, robust. The kind of code "
        "you'd ship without changes.\n\n"
        "Output EXACTLY two lines:\n"
        "Line 1: ``SCORE: <integer 1-5>``\n"
        "Line 2: One short sentence (max 25 words) justifying the score.\n\n"
        "Do not add preamble, headings, or markdown."
    )


# Back-compat constant for callers / tests that expect the Python
# judge system prompt verbatim.
JUDGE_SYSTEM = build_judge_system("Python")


_JUDGE_SCORE_RE = re.compile(
    r"^\s*SCORE\s*:\s*(\d)\b", re.IGNORECASE | re.MULTILINE,
)


def build_judge_messages(
    task: str,
    code: str,
    language: str = "Python",
    fence: str = "python",
) -> list[dict]:
    """Construct the conversation for the judge model. Keeps the
    prompt short on purpose — judge is a single fast call, not an
    agent loop.

    ``language`` / ``fence`` are language-aware:

    - ``language`` is interpolated into the reviewer persona + rubric
      so the judge rates the right idioms.
    - ``fence`` is the markdown fence tag (``python``, ``rust``,
      ``cpp``, ``csharp``, ``c``, ``go``, ...) so the judge doesn't
      see C / Rust / Go code wrapped in a ```python fence and
      auto-penalize it for not parsing as Python.

    Defaults preserve the pre-mlang Python-only behavior. The
    coder_bench caller derives both from the question's ``sandbox_path``
    extension via ``judge_lang_for_path``.
    """
    user = (
        f"TASK GIVEN TO THE JUNIOR ENGINEER:\n{task.strip()}\n\n"
        f"CODE THE JUNIOR PRODUCED:\n```{fence}\n{code}\n```\n\n"
        "Score the code per the rubric. Two lines only."
    )
    return [
        {"role": "system", "content": build_judge_system(language)},
        {"role": "user", "content": user},
    ]


def build_post_test_judge_system(language: str = "Python") -> str:
    """System prompt for the v1.0.1 **post-test judge**.

    Fired only when ``passes_algorithm == False``. The judge gets:
      - the task
      - the produced code
      - the name of a test that failed and the (truncated) failure msg

    and rates 1-5 on EDGE-CASE / ROBUSTNESS quality specifically.

    The rubric is intentionally different from the read-only judge.
    The read-only judge can score a buggy-but-clean piece of code at
    5 because the bug is invisible at a glance. The post-test judge
    sees evidence the code failed and is asked: *should the author
    have anticipated this?* Diverging scores from the two judges is
    the operational signal that a model writes camouflaged bugs.
    """
    return (
        f"You are a senior {language} code reviewer doing a POST-MORTEM "
        "review of code that failed a test. You see:\n"
        "- The task the engineer was given\n"
        "- The code they produced\n"
        "- The name of a test that failed and the failure message\n\n"
        "Score the code on a 1-5 scale specifically on EDGE-CASE / "
        "ROBUSTNESS quality:\n\n"
        "1 — Code is broken at a fundamental level; this failure is "
        "one of many bugs.\n"
        "2 — The code obviously missed this case; basic review would "
        "have caught it.\n"
        "3 — The code is correct for the main case but didn't "
        "anticipate this edge.\n"
        "4 — The code mostly handles edge cases; the gap that failed "
        "is narrow / requires deep thinking to anticipate.\n"
        "5 — The code IS robust against this case. This suggests an "
        "oracle / spec issue, not the code's fault.\n\n"
        "Output EXACTLY two lines:\n"
        "Line 1: ``SCORE: <integer 1-5>``\n"
        "Line 2: One short sentence (max 25 words) explaining whether "
        "the code should have anticipated this failure.\n\n"
        "Do not add preamble, headings, or markdown."
    )


def build_robustness_judge_system(language: str = "Python") -> str:
    """System prompt for the v1.0.1 audit judge's **robustness mode**.

    Fires on trials with ``passes_algorithm == True``. The judge does
    NOT see a failure — its job is to identify ONE plausible edge case
    the visible test suite does not cover. Captures latent bugs in
    passing code: e.g. a sort that happens to produce correct output
    despite a buggy pivot, a parser that handles the documented inputs
    but breaks on a documented-but-untested edge.

    The rubric scales the audit judge's "is this code actually robust?"
    verdict against the read-only judge's clarity / idiomatic verdict —
    cross-judge cross-model coverage on every trial.
    """
    return (
        f"You are a senior {language} code reviewer doing a "
        "ROBUSTNESS AUDIT on code that already passed its visible "
        "test suite.\n\n"
        "Real-world code must survive edge cases not in any visible "
        "test. Identify ONE plausible edge case this code does not "
        "handle, if any exists.\n\n"
        "Score on a 1-5 scale:\n\n"
        "5 — No plausible unhandled edge case. The code is robust.\n"
        "4 — One narrow edge case the visible tests don't cover; an "
        "experienced reviewer might catch it on careful read.\n"
        "3 — One obvious edge case missed; basic review would have "
        "flagged it.\n"
        "2 — Multiple edge cases missed; the code's robustness is "
        "shaky despite passing the visible tests.\n"
        "1 — The code happens to pass these specific tests but is "
        "broken at a deeper level (wrong algorithm, dead branches, "
        "etc.).\n\n"
        "Output EXACTLY two lines:\n"
        "Line 1: ``SCORE: <integer 1-5>``\n"
        "Line 2: One short sentence (max 25 words) naming the edge "
        "case OR confirming none.\n\n"
        "Do not add preamble, headings, or markdown."
    )


def build_robustness_judge_messages(
    task: str,
    code: str,
    language: str = "Python",
    fence: str = "python",
) -> list[dict]:
    """Construct the conversation for the v1.0.1 audit judge's
    robustness mode (passing trials). Mirrors
    ``build_post_test_judge_messages`` but does NOT include any
    failure context — the judge's job is to FIND the missing edge
    case, not to react to one.
    """
    user = (
        f"TASK GIVEN TO THE JUNIOR ENGINEER:\n{task.strip()}\n\n"
        f"CODE THE JUNIOR PRODUCED (this code passed all visible tests):\n"
        f"```{fence}\n{code}\n```\n\n"
        "Score per the rubric. Two lines only."
    )
    return [
        {"role": "system", "content": build_robustness_judge_system(language)},
        {"role": "user", "content": user},
    ]


def build_post_test_judge_messages(
    task: str,
    code: str,
    failing_test_name: str,
    failing_msg: str,
    language: str = "Python",
    fence: str = "python",
    msg_chars: int = 200,
) -> list[dict]:
    """Construct the conversation for the v1.0.1 post-test judge.

    Mirrors ``build_judge_messages`` but adds the failure context.
    ``failing_msg`` is truncated to ``msg_chars`` characters so the
    judge prompt stays bounded — the goal is for the judge to know
    WHAT failed, not to debug the C compiler's diagnostic verbatim.
    """
    msg_trunc = (failing_msg or "").strip()[:msg_chars]
    user = (
        f"TASK GIVEN TO THE JUNIOR ENGINEER:\n{task.strip()}\n\n"
        f"CODE THE JUNIOR PRODUCED:\n```{fence}\n{code}\n```\n\n"
        f"TEST THAT FAILED: {failing_test_name}\n"
        f"FAILURE MESSAGE (first {msg_chars} chars):\n{msg_trunc}\n\n"
        "Score per the rubric. Two lines only."
    )
    return [
        {"role": "system", "content": build_post_test_judge_system(language)},
        {"role": "user", "content": user},
    ]


def parse_judge_response(text: str) -> tuple[Optional[float], str]:
    """Extract ``(score, rationale)`` from the judge's reply.
    ``score`` is a float (1.0–5.0) when parseable, else None.
    ``rationale`` is everything after the SCORE line, lightly
    cleaned. Tolerant of:

    - Extra blank lines
    - Trailing markdown or quotes
    - Score lines emitted as ``Score:`` / ``score = 4`` / ``4/5``
    """
    if not text or not isinstance(text, str):
        return None, ""
    body = text.strip()
    m = _JUDGE_SCORE_RE.search(body)
    if m is None:
        # Fall back to looking for "<n>/5" or "score = <n>".
        alt = re.search(
            r"\b([1-5])\s*[/\\]\s*5\b", body,
        ) or re.search(
            r"score\s*[=:]\s*([1-5])\b", body, re.IGNORECASE,
        )
        if alt is None:
            return None, body[:200]
        score = float(alt.group(1))
        # Everything else is rationale.
        rationale = re.sub(
            r"\b([1-5])\s*[/\\]\s*5\b", "", body,
        ).strip()
        return score, rationale[:200]
    score = float(m.group(1))
    # Rationale = next non-empty line(s) after the SCORE line.
    after = body[m.end():].strip()
    rationale = after.split("\n", 1)[0].strip() if after else ""
    return score, rationale[:200]


# ============================================================== #
# JSON writer — append-only so Ctrl-C preserves prior trials
# ============================================================== #

def append_trial(trials_path: Path, trial: CoderTrial) -> None:
    """Append one trial as a JSON line. Creates the parent dir
    + file if missing. Used by the live bench to checkpoint after
    every trial so partial runs aren't a total loss.
    """
    trials_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trials_path, "a", encoding="utf-8") as f:
        json.dump(trial.to_dict(), f, default=str)
        f.write("\n")


def load_trials(trials_path: Path) -> list[CoderTrial]:
    """Read every JSON line back into ``CoderTrial`` instances.
    Tolerant of partial files (last line truncated); reports
    silently as fewer trials, not as an error.
    """
    if not trials_path.is_file():
        return []
    out: list[CoderTrial] = []
    with open(trials_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping malformed line: %s", line[:80])
                continue
            try:
                out.append(CoderTrial(**data))
            except TypeError:
                log.warning(
                    "skipping line with unknown fields: %s",
                    list(data.keys()),
                )
    return out


# ============================================================== #
# Dry-run stub — emulates a model writing a file via the sandbox
# tool. Used by the harness self-tests and by ``--dry-run`` so the
# pipeline can be exercised without cloud spend.
# ============================================================== #

def make_dry_run_loop_runner(*, file_path: str, content: str,
                             iterations: int = 1,
                             prompt_tokens: int = 500,
                             completion_tokens: int = 200):
    """Build a stub agent-loop runner that simulates the coder
    calling ``write_file`` once with the supplied content. The
    harness's smoke tests + ``--dry-run`` mode use this so every
    trial path is exercised without hitting the cloud.

    Tracks per-call counters via the closure so the bench's token
    + iteration metrics still produce sensible numbers on dry runs.
    """
    def _runner(payload, cwd, *, config, tool_specs, chat_fn,
                tool_executor, on_iter=None, on_tool=None,
                preseed_builder=None):
        # Simulate one write_file call. Mirrors the real
        # ``run_loop`` call shape (3 positional args:
        # name, args_json_str, cwd) so signature drift between the
        # dry-run path and the live path is caught at smoke time
        # rather than after a full cloud run.
        args = json.dumps({"path": file_path, "content": content})
        tool_output = tool_executor("write_file", args, cwd)
        if on_tool is not None:
            on_tool("write_file", args, tool_output, 5, None)
        if on_iter is not None:
            for i in range(iterations):
                resp = {
                    "choices": [{
                        "message": {"role": "assistant",
                                     "content": f"WROTE {file_path}."},
                    }],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    },
                }
                on_iter(i, payload, resp, 50)
        final_text = (
            f"WROTE {file_path}. Implements the task as requested."
        )
        return {
            "final": {"choices": [
                {"role": "assistant", "content": final_text,
                 "message": {"role": "assistant", "content": final_text}},
            ]},
        }
    return _runner


__all__ = [
    "BenchQuestion",
    "CoderTrial",
    "JUDGE_SYSTEM",
    "OracleResult",
    "TIERS",
    "append_trial",
    "build_judge_messages",
    "build_judge_system",
    "build_post_test_judge_messages",
    "build_post_test_judge_system",
    "build_robustness_judge_messages",
    "build_robustness_judge_system",
    "count_code_lines",
    "estimate_cost",
    "judge_lang_for_path",
    "load_questions",
    "parse_constraint_tests",
    "load_trials",
    "make_dry_run_loop_runner",
    "measure_complexity",
    "parse_judge_response",
    "run_pytest_against_sandbox",
]
