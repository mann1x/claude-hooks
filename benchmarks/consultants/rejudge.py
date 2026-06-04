#!/usr/bin/env python3
"""Offline re-judge + comparative-ladder tool for the coder bench.

Two modes, both **offline** — they read the persisted per-trial
sandboxes produced by a prior ``coder_bench`` run (the real
``solution.*`` source survives under
``<sandbox_dir>/.claude-hooks/consultants/bench/coder-out/``) and call
a *second*, independent judge model. No coder is re-run, so this costs
only judge calls.

Motivation (2026-06-04): the easy-suite rescore (commit ``5f5af00``)
was judged solely by ``kimi-k2.6`` — which is also a competitor in the
cohort. The user asked for (a) a second, independent judge
(``gemini-3-flash-preview:cloud``) to re-score existing results, and
(b) a comparative "ladder" that shows ONE judge all models' code for a
question at once and asks it to rank them best->worst with ties,
instead of only isolated 1-5 scores.

    # second judge, absolute re-score (additive — never mutates the
    # kimi fields; writes quality_secondary_* alongside)
    rejudge.py --rescore \
        --trials results/2026-06-03/coder_easy/trials.jsonl \
        --questions-dir questions/coder_easy \
        --lang c,cpp,csharp,python

    # comparative ladder (one judge ranks all models per question)
    rejudge.py --ladder \
        --trials results/2026-06-03/coder_easy/trials.jsonl \
        --questions-dir questions/coder_easy \
        --lang c,cpp,csharp,python

``gemini-3-flash-preview:cloud`` is a reasoning model: it returns
EMPTY content at a low ``num_predict`` and only emits the ``SCORE:`` /
``RANKING:`` line once it has enough budget to think. The judge calls
in ``coder_bench`` set no ``num_predict``; this tool injects
``options.num_predict`` (default 800) so the gemini judge has room.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import socket
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import (  # noqa: E402
    BenchQuestion,
    assign_ladder_labels,
    build_judge_messages,
    build_ladder_messages,
    judge_lang_for_path,
    load_questions,
    parse_judge_response,
    parse_ladder_response,
)

log = logging.getLogger("benchmarks.consultants.rejudge")

DEFAULT_OLLAMA_BASE = "http://192.168.178.2:11433"
DEFAULT_JUDGE_MODEL = "gemini-3-flash-preview:cloud"
# gemini-3-flash-preview is a reasoning model; it emits empty content
# below ~400 tokens of budget. 800 gives the SCORE/RANKING line plus a
# one-line rationale comfortably.
DEFAULT_NUM_PREDICT = 800
# coder_bench writes the produced source under this fixed sub-path of
# each trial's sandbox_dir (the synthetic sid is always "bench").
_PRODUCED_SUBDIR = Path(".claude-hooks") / "consultants" / "bench" / "coder-out"


# ============================================================== #
# Provenance
# ============================================================== #

def _git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT, stderr=subprocess.DEVNULL, timeout=2.0,
        )
        return out.decode("utf-8").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# ============================================================== #
# Trial loading (raw dicts — preserve every key so we can add the
# secondary-judge fields without dropping unknown columns the way
# harness.load_trials would via CoderTrial(**data)).
# ============================================================== #

def _iter_trials_files(trials_arg: Path):
    """Yield trials.jsonl paths from a file or results dir.

    - A file: that file.
    - A dir with a top-level ``trials.jsonl``: that merged file.
    - Otherwise a dir: every ``*/trials.jsonl`` one level down.
    """
    if trials_arg.is_file():
        yield trials_arg
        return
    if trials_arg.is_dir():
        merged = trials_arg / "trials.jsonl"
        if merged.is_file():
            yield merged
            return
        found = sorted(trials_arg.glob("*/trials.jsonl"))
        if not found:
            raise FileNotFoundError(
                f"no trials.jsonl under {trials_arg}"
            )
        yield from found
        return
    raise FileNotFoundError(f"trials path not found: {trials_arg}")


def load_raw_trials(trials_arg: Path) -> list[dict]:
    """Load every trial row as a raw dict from one or more
    trials.jsonl files. Tolerant of truncated last lines."""
    rows: list[dict] = []
    for p in _iter_trials_files(trials_arg):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skip malformed line in %s: %s", p, line[:80])
    return rows


def _language_of(question_id: str) -> str:
    """First dash-token of the question id (mirrors analyze.py)."""
    return question_id.split("-", 1)[0] if question_id else ""


def _resolve_source(trial: dict) -> tuple[Optional[str], str]:
    """Read the produced source for a trial. Returns ``(code, "")`` on
    success or ``(None, reason)`` when no source is recoverable
    (didn't compile / no files_written / file gone)."""
    sandbox_dir = trial.get("sandbox_dir") or ""
    if not sandbox_dir:
        return None, "no sandbox_dir"
    files = trial.get("files_written") or []
    # Prefer the file matching the question's expected extension; else
    # the first written file.
    candidates = [f.get("path") for f in files if f.get("path")]
    sandbox = Path(sandbox_dir)
    produced = sandbox / _PRODUCED_SUBDIR
    search_roots = [produced, sandbox]
    for name in candidates:
        for root in search_roots:
            fp = root / name
            if fp.is_file():
                try:
                    return fp.read_text(encoding="utf-8", errors="replace"), ""
                except OSError as e:
                    return None, f"read error: {e}"
    # Fallback: rglob for any solution.* under the trial dir.
    if sandbox.is_dir():
        for fp in sorted(sandbox.rglob("solution.*")):
            if fp.is_file() and not fp.name.endswith(".xml"):
                try:
                    return fp.read_text(encoding="utf-8", errors="replace"), ""
                except OSError as e:
                    return None, f"read error: {e}"
    return None, "no produced source on disk"


def _question_map(questions_dir: Path) -> dict[str, BenchQuestion]:
    qs = load_questions(questions_dir, require_oracle=False)
    return {q.id: q for q in qs}


# ============================================================== #
# Judge client
# ============================================================== #

def _make_judge(model: str, ollama_base: str, timeout_s: float):
    from claude_hooks.get_advice.chat_client import make_agent_chat_client
    return make_agent_chat_client(
        model, ollama_base, timeout_s=timeout_s, max_retries=3,
    )


def _judge_call(client, model: str, messages: list, num_predict: int,
                empty_retries: int = 1) -> tuple[str, str]:
    """One judge call with an empty-content retry. Returns
    ``(text, "")`` or ``("", reason)``."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": int(num_predict)},
    }
    attempts = 1 + max(0, empty_retries)
    last_reason = "empty"
    for _ in range(attempts):
        try:
            resp = client.chat(payload)
        except Exception as e:  # noqa: BLE001 — judge errors are soft
            last_reason = f"judge raised: {e}"
            continue
        if not isinstance(resp, dict):
            last_reason = "non-dict response"
            continue
        choices = resp.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            last_reason = "no choices"
            continue
        text = (choices[0].get("message") or {}).get("content") or ""
        if text.strip():
            return text, ""
        last_reason = "empty content"
    return "", last_reason


def _judge_many(work: list, *, make_client, model: str, num_predict: int,
                concurrency: int) -> dict:
    """Run a batch of judge calls. ``work`` is a list of
    ``(key, messages)``; returns ``{key: (text, reason)}``.

    ``concurrency <= 1`` reuses a single client sequentially (preserves
    the original behavior + keeps the offline tests' single stub
    deterministic). ``concurrency > 1`` fans out across a thread pool
    with one thread-local client each, so the 840-easy + 420-med
    re-score finishes in tens of minutes instead of hours. All workers
    hit the same Ollama-Pro endpoint, so keep concurrency modest
    (4–8) to avoid re-triggering the cloud-side throttle the shared
    judge already strains under.
    """
    results: dict = {}
    if concurrency <= 1:
        client = make_client()
        for key, msgs in work:
            results[key] = _judge_call(client, model, msgs, num_predict)
        return results
    import threading
    from concurrent.futures import ThreadPoolExecutor

    tl = threading.local()

    def _worker(item):
        key, msgs = item
        client = getattr(tl, "client", None)
        if client is None:
            client = make_client()
            tl.client = client
        return key, _judge_call(client, model, msgs, num_predict)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for key, res in ex.map(_worker, work):
            results[key] = res
    return results


# ============================================================== #
# Mode: --rescore (absolute second-judge re-score)
# ============================================================== #

def cmd_rescore(args) -> int:
    if getattr(args, "report_only", False):
        return _rescore_report_only(args)
    if not args.questions_dir:
        log.error("--questions-dir is required for --rescore (judging mode)")
        return 2
    trials_arg = Path(args.trials)
    questions_dir = Path(args.questions_dir)
    lang_filter = (
        {x.strip() for x in args.lang.split(",") if x.strip()}
        if args.lang else None
    )
    out_path = Path(args.output) if args.output else (
        (trials_arg if trials_arg.is_dir() else trials_arg.parent)
        / "trials-rejudged.jsonl"
    )
    report_path = Path(args.report) if args.report else out_path.parent / "rescore-report.md"

    rows = load_raw_trials(trials_arg)
    qmap = _question_map(questions_dir)
    log.info("loaded %d trials, %d questions", len(rows), len(qmap))

    concurrency = max(1, int(getattr(args, "concurrency", 1) or 1))
    retry = bool(getattr(args, "retry_unparseable", False))

    # Pass 1: build the output rows in order. Rows outside the filter or
    # past --limit pass through untouched; rows with no recoverable
    # source are skipped immediately; the rest queue a judge call keyed
    # by output index. In --retry-unparseable mode the input is an
    # already-rejudged file and only rows that previously yielded NO
    # numeric score (and have a recoverable source — not "skipped:") are
    # re-judged, typically at a higher --num-predict; every other row
    # keeps its existing secondary score untouched.
    rejudged: list[dict] = [dict(r) for r in rows]
    n_skipped = 0
    processed = 0
    work: list = []
    for idx, row in enumerate(rows):
        qid = row.get("question_id", "")
        lang = _language_of(qid)
        out_row = rejudged[idx]
        if lang_filter is not None and lang not in lang_filter:
            continue
        if retry:
            existing = out_row.get("quality_secondary_score")
            rat = str(out_row.get("quality_secondary_rationale", ""))
            if isinstance(existing, (int, float)):
                continue  # already has a numeric score — keep it
            if rat.startswith("skipped:"):
                continue  # no recoverable source — can't retry
        if args.limit and processed >= args.limit:
            continue
        processed += 1

        q = qmap.get(qid)
        code, reason = _resolve_source(row)
        if q is None:
            reason = reason or "question not in suite dir"
        if code is None or q is None:
            out_row["quality_secondary_score"] = None
            out_row["quality_secondary_judge_model"] = args.judge_model
            out_row["quality_secondary_rationale"] = f"skipped: {reason}"
            n_skipped += 1
            continue

        language, fence = judge_lang_for_path(q.sandbox_path)
        msgs = build_judge_messages(q.task, code, language=language, fence=fence)
        work.append((idx, msgs))

    # Pass 2: run the judge calls (sequential or thread-pooled).
    log.info("judging %d trials (concurrency=%d, model=%s)",
             len(work), concurrency, args.judge_model)
    judged = _judge_many(
        work,
        make_client=lambda: _make_judge(
            args.judge_model, args.ollama_base, args.timeout_s),
        model=args.judge_model, num_predict=args.num_predict,
        concurrency=concurrency,
    )

    # Pass 3: fold results back in.
    n_scored = 0
    for idx, (text, jreason) in judged.items():
        out_row = rejudged[idx]
        out_row["quality_secondary_judge_model"] = args.judge_model
        if not text:
            out_row["quality_secondary_score"] = None
            out_row["quality_secondary_rationale"] = f"judge: {jreason}"
            n_skipped += 1
            log.warning("[%s × %s] no score: %s",
                        out_row.get("question_id"), out_row.get("model"), jreason)
            continue
        score, rationale = parse_judge_response(text)
        out_row["quality_secondary_score"] = score
        out_row["quality_secondary_rationale"] = rationale
        n_scored += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rejudged:
            json.dump(r, f, default=str)
            f.write("\n")

    meta = {
        "tool": "rejudge --rescore",
        "primary_judge": _detect_primary_judge(rows),
        "secondary_judge": args.judge_model,
        "num_predict": args.num_predict,
        "lang_filter": sorted(lang_filter) if lang_filter else None,
        "n_input": len(rows),
        "n_scored": n_scored,
        "n_skipped": n_skipped,
        "git_commit": _git_commit(),
        "host": socket.gethostname(),
        "generated_at": _now(),
        "trials_rejudged": str(out_path),
    }
    report = render_rescore_report(rejudged, meta, lang_filter)
    report_path.write_text(report, encoding="utf-8")
    print(f"rescored {n_scored} (skipped {n_skipped}) -> {out_path}")
    print(f"report -> {report_path}")
    return 0


def _rescore_report_only(args) -> int:
    """Re-render the rescore report from an existing trials-rejudged.jsonl
    (rows already carry quality_secondary_*). No judging, no cloud spend —
    used to refresh report formatting/accounting without re-spending the
    judge calls."""
    trials_arg = Path(args.trials)
    rows = load_raw_trials(trials_arg)
    lang_filter = (
        {x.strip() for x in args.lang.split(",") if x.strip()}
        if args.lang else None
    )
    secondary = next(
        (r["quality_secondary_judge_model"] for r in rows
         if r.get("quality_secondary_judge_model")),
        args.judge_model,
    )
    report_path = Path(args.report) if args.report else (
        (trials_arg if trials_arg.is_dir() else trials_arg.parent)
        / "rescore-report.md")
    meta = {
        "tool": "rejudge --rescore --report-only",
        "primary_judge": _detect_primary_judge(rows),
        "secondary_judge": secondary,
        "num_predict": args.num_predict,
        "lang_filter": sorted(lang_filter) if lang_filter else None,
        "n_input": len(rows),
        "git_commit": _git_commit(),
        "host": socket.gethostname(),
        "generated_at": _now(),
        "trials_rejudged": str(trials_arg),
    }
    report_path.write_text(
        render_rescore_report(rows, meta, lang_filter), encoding="utf-8")
    print(f"re-rendered report -> {report_path}")
    return 0


def _detect_primary_judge(rows: list[dict]) -> str:
    """Best-effort: the model that produced the original quality_score.
    The trial schema doesn't store it per-row, so fall back to the
    known easy/med default."""
    return "kimi-k2.6:cloud"


def _avg(vals: list) -> Optional[float]:
    nums = [v for v in vals if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


def _fmt(x: Optional[float]) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "—"


def _fmt_delta(a: Optional[float], b: Optional[float]) -> str:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        d = b - a
        return f"{d:+.2f}"
    return "—"


def render_rescore_report(rows: list[dict], meta: dict,
                          lang_filter: Optional[set]) -> str:
    primary = meta["primary_judge"]
    secondary = meta["secondary_judge"]
    # Rows the second judge was actually invoked on carry the
    # judge-model key. Categorize honestly: a numeric score, an
    # unparseable judge reply (text present, no SCORE line — common
    # with reasoning models), or no source / silent judge.
    processed = [r for r in rows if "quality_secondary_judge_model" in r]
    scored = [r for r in processed
              if isinstance(r.get("quality_secondary_score"), (int, float))]
    nosource = [r for r in processed
                if r.get("quality_secondary_score") is None
                and str(r.get("quality_secondary_rationale", "")).startswith(
                    ("skipped:", "judge:"))]
    unparseable = [r for r in processed
                   if r.get("quality_secondary_score") is None
                   and r not in nosource]

    by_model_p: dict[str, list] = defaultdict(list)
    by_model_s: dict[str, list] = defaultdict(list)
    by_lang_p: dict[str, list] = defaultdict(list)
    by_lang_s: dict[str, list] = defaultdict(list)
    for r in scored:
        m = r.get("model", "?")
        lang = _language_of(r.get("question_id", ""))
        p = r.get("quality_score")
        s = r.get("quality_secondary_score")
        by_model_p[m].append(p)
        by_model_s[m].append(s)
        by_lang_p[lang].append(p)
        by_lang_s[lang].append(s)

    lines: list[str] = []
    lines.append(f"# Cross-judge re-score — {primary} vs {secondary}")
    lines.append("")
    lines.append(
        f"Second, independent judge **{secondary}** re-scored the persisted "
        f"code from a prior `coder_bench` run, using the same absolute 1–5 "
        f"rubric the primary judge (**{primary}**) used. Additive: the "
        f"original `quality_score` is untouched; gemini's score lands in "
        f"`quality_secondary_score`."
    )
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- generated: `{meta['generated_at']}`  ·  git `{meta['git_commit']}`  ·  host `{meta['host']}`")
    lines.append(f"- secondary judge: `{secondary}`  (num_predict={meta['num_predict']})")
    lf = meta.get("lang_filter")
    lines.append(f"- language filter: {', '.join(lf) if lf else 'all'}")
    lines.append(
        f"- input trials: {meta['n_input']}  ·  judged: {len(processed)}  ·  "
        f"**numeric score: {len(scored)}**  ·  unparseable judge reply "
        f"(no SCORE line): {len(unparseable)}  ·  no source / silent: "
        f"{len(nosource)}"
    )
    if unparseable:
        pct = 100.0 * len(unparseable) / max(1, len(processed))
        lines.append(
            f"  - the {len(unparseable)} unparseable ({pct:.1f}%) are "
            f"`{secondary}` reasoning replies that omitted a parseable "
            f"`SCORE:` line; they are excluded from every average below."
        )
    lines.append(f"- rejudged jsonl: `{meta['trials_rejudged']}`")
    lines.append("")

    # Overall
    all_p = _avg([r.get("quality_score") for r in scored])
    all_s = _avg([r.get("quality_secondary_score") for r in scored])
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **{primary}** mean quality: **{_fmt(all_p)}**")
    lines.append(f"- **{secondary}** mean quality: **{_fmt(all_s)}**")
    lines.append(f"- cohort-wide delta (gemini − kimi): **{_fmt_delta(all_p, all_s)}**")
    lines.append("")

    # Per-language
    lines.append("## Per-language (mean quality)")
    lines.append("")
    lines.append(f"| language | n | {primary} | {secondary} | Δ (gem−kimi) |")
    lines.append("|---|---:|---:|---:|---:|")
    for lang in sorted(by_lang_p):
        p = _avg(by_lang_p[lang]); s = _avg(by_lang_s[lang])
        lines.append(
            f"| {lang} | {len(by_lang_p[lang])} | {_fmt(p)} | {_fmt(s)} | {_fmt_delta(p, s)} |"
        )
    lines.append("")

    # Per-model
    lines.append("## Per-model (mean quality)")
    lines.append("")
    lines.append(f"| model | n | {primary} | {secondary} | Δ (gem−kimi) |")
    lines.append("|---|---:|---:|---:|---:|")
    for m in sorted(by_model_p, key=lambda k: (_avg(by_model_s[k]) or 0), reverse=True):
        p = _avg(by_model_p[m]); s = _avg(by_model_s[m])
        lines.append(
            f"| `{m}` | {len(by_model_p[m])} | {_fmt(p)} | {_fmt(s)} | {_fmt_delta(p, s)} |"
        )
    lines.append("")

    # Self-judge bias check: where the COMPETITOR model is the primary
    # judge's own family (kimi), does the primary judge inflate its own
    # code relative to how gemini scores it, beyond the cohort-wide
    # delta? A self-favoring primary judge shows a more-negative
    # (gemini lower) delta on its OWN rows than on the cohort.
    judge_family = primary.split(":")[0].split("-")[0]  # "kimi"
    self_rows = [r for r in scored
                 if r.get("model", "").split(":")[0].split("-")[0] == judge_family]
    lines.append("## Self-judge bias check")
    lines.append("")
    if self_rows:
        sp = _avg([r.get("quality_score") for r in self_rows])
        ss = _avg([r.get("quality_secondary_score") for r in self_rows])
        self_delta = (ss - sp) if isinstance(sp, (int, float)) and isinstance(ss, (int, float)) else None
        cohort_delta = (all_s - all_p) if isinstance(all_p, (int, float)) and isinstance(all_s, (int, float)) else None
        lines.append(
            f"`{primary}` was the sole primary judge **and** a competitor. "
            f"On its own {len(self_rows)} rows (coder family = `{judge_family}`):"
        )
        lines.append("")
        lines.append(f"- {primary} self-score: **{_fmt(sp)}**  ·  {secondary} on the same code: **{_fmt(ss)}**")
        lines.append(f"- self delta (gem−kimi): **{_fmt_delta(sp, ss)}**  ·  cohort delta: **{_fmt_delta(all_p, all_s)}**")
        if self_delta is not None and cohort_delta is not None:
            gap = self_delta - cohort_delta
            verdict = (
                "primary judge **inflates its own code** vs the second judge "
                "more than the cohort average — evidence of self-judge bias"
                if gap < -0.15 else
                "no material self-favoring beyond the cohort-wide judge gap"
                if abs(gap) <= 0.15 else
                "primary judge is HARSHER on its own code than the cohort — "
                "no self-inflation"
            )
            lines.append(f"- gap (self − cohort): **{gap:+.2f}** → {verdict}")
    else:
        lines.append(
            f"No rows where the coder family matches the primary judge "
            f"(`{judge_family}`); self-judge bias not directly measurable on "
            f"this cohort."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_Numbers recomputable from the rejudged jsonl: mean of "
        "`quality_score` (primary) and `quality_secondary_score` (gemini) "
        "grouped by model / language._"
    )
    lines.append("")
    return "\n".join(lines)


# ============================================================== #
# Mode: --ladder (comparative ranking)
# ============================================================== #

def cmd_ladder(args) -> int:
    if not args.questions_dir:
        log.error("--questions-dir is required for --ladder")
        return 2
    trials_arg = Path(args.trials)
    questions_dir = Path(args.questions_dir)
    lang_filter = (
        {x.strip() for x in args.lang.split(",") if x.strip()}
        if args.lang else None
    )
    out_dir = (trials_arg if trials_arg.is_dir() else trials_arg.parent)
    out_path = Path(args.output) if args.output else out_dir / "ladder.jsonl"
    report_path = Path(args.report) if args.report else out_dir / "ladder-report.md"

    rows = load_raw_trials(trials_arg)
    qmap = _question_map(questions_dir)

    # Group code by (qid -> model -> code). Keep the LAST trial per
    # (qid, model). Also remember pass-rate + primary/secondary quality
    # for the cross-check table.
    code_by_q: dict[str, dict[str, str]] = defaultdict(dict)
    pass_by_model: dict[str, list] = defaultdict(list)
    pq_by_model: dict[str, list] = defaultdict(list)
    sq_by_model: dict[str, list] = defaultdict(list)
    for r in rows:
        qid = r.get("question_id", "")
        lang = _language_of(qid)
        if lang_filter is not None and lang not in lang_filter:
            continue
        if qid not in qmap:
            continue
        model = r.get("model", "?")
        pass_by_model[model].append(1 if r.get("passes_algorithm") or r.get("passes_tests") else 0)
        pq_by_model[model].append(r.get("quality_score"))
        sq_by_model[model].append(r.get("quality_secondary_score"))
        code, _reason = _resolve_source(r)
        if code is not None:
            code_by_q[qid][model] = code

    concurrency = max(1, int(getattr(args, "concurrency", 1) or 1))
    retry = bool(getattr(args, "retry_unparseable", False))

    # --retry-unparseable: load the prior ladder.jsonl and re-rank ONLY
    # the questions whose previous ranking did not parse cleanly (run at
    # a higher --num-predict). Valid prior rankings are preserved and
    # merged back below.
    existing_recs: list[dict] = []
    retry_qids: Optional[set] = None
    if retry:
        existing_recs = load_raw_trials(out_path)
        retry_qids = {r.get("question_id") for r in existing_recs
                      if not r.get("valid")}
        log.info("retry: %d prior rankings, %d invalid to redo",
                 len(existing_recs), len(retry_qids))

    # Build one ladder call per question with >=2 candidate solutions.
    ladder_qids = [q for q in sorted(code_by_q)
                   if len(code_by_q[q]) >= 2
                   and (retry_qids is None or q in retry_qids)]
    label_models: dict[str, list] = {}
    work: list = []
    for qid in ladder_qids:
        models_here = sorted(code_by_q[qid])
        q = qmap[qid]
        language, fence = judge_lang_for_path(q.sandbox_path)
        label_model = assign_ladder_labels(qid, models_here)
        label_models[qid] = label_model
        labeled_solutions = [(lab, code_by_q[qid][m]) for lab, m in label_model]
        msgs = build_ladder_messages(q.task, labeled_solutions, language, fence)
        work.append((qid, msgs))

    log.info("laddering %d questions (concurrency=%d, model=%s)",
             len(work), concurrency, args.judge_model)
    judged = _judge_many(
        work,
        make_client=lambda: _make_judge(
            args.judge_model, args.ollama_base, args.timeout_s),
        model=args.judge_model, num_predict=args.num_predict,
        concurrency=concurrency,
    )

    per_question: list[dict] = []
    for qid in ladder_qids:
        lang = _language_of(qid)
        label_model = label_models[qid]
        label_to_model = {lab: m for lab, m in label_model}
        labels = [lab for lab, _ in label_model]
        text, jreason = judged.get(qid, ("", "not dispatched"))
        parsed = parse_ladder_response(text, labels)
        rec = {
            "question_id": qid,
            "language": lang,
            "labels": label_to_model,
            "raw_ranking": parsed.raw,
            "rationale": parsed.rationale,
            "valid": parsed.valid,
            "missing": parsed.missing,
            "unknown": parsed.unknown,
            "duplicated": parsed.duplicated,
            "judge_silent_reason": jreason if not text else "",
            "model_ranks": {},
        }
        for lab, fr in (parsed.ranks or {}).items():
            m = label_to_model.get(lab)
            if m is not None:
                rec["model_ranks"][m] = fr
        if not parsed.ranks:
            log.warning("[ladder %s] no ranking: %s", qid, jreason or "unparseable")
        per_question.append(rec)

    # Retry mode: merge the freshly-redone rankings over the preserved
    # prior ones (replace by question_id) so the output is the full set.
    if retry:
        new_by_qid = {r["question_id"]: r for r in per_question}
        seen = set()
        merged = []
        for r in existing_recs:
            qid = r.get("question_id")
            seen.add(qid)
            merged.append(new_by_qid.get(qid, r))
        merged += [r for r in per_question if r["question_id"] not in seen]
        per_question = merged

    # Aggregate fractional ranks over the FINAL question set (so retry
    # totals reflect preserved + recovered rankings).
    ranks_by_model: dict[str, list[float]] = defaultdict(list)
    ranks_by_model_lang: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    n_q = len(per_question)
    n_q_ok = 0
    for rec in per_question:
        if rec.get("valid"):
            n_q_ok += 1
        rlang = rec.get("language", "")
        for m, fr in (rec.get("model_ranks") or {}).items():
            ranks_by_model[m].append(fr)
            ranks_by_model_lang[rlang][m].append(fr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in per_question:
            json.dump(rec, f, default=str)
            f.write("\n")

    meta = {
        "tool": "rejudge --ladder",
        "judge": args.judge_model,
        "num_predict": args.num_predict,
        "lang_filter": sorted(lang_filter) if lang_filter else None,
        "n_questions": n_q,
        "n_questions_valid": n_q_ok,
        "git_commit": _git_commit(),
        "host": socket.gethostname(),
        "generated_at": _now(),
        "ladder_jsonl": str(out_path),
    }
    pass_rate = {m: (sum(v) / len(v) if v else None) for m, v in pass_by_model.items()}
    pq = {m: _avg(v) for m, v in pq_by_model.items()}
    sq = {m: _avg(v) for m, v in sq_by_model.items()}
    report = render_ladder_report(
        ranks_by_model, ranks_by_model_lang, meta, pass_rate, pq, sq,
    )
    report_path.write_text(report, encoding="utf-8")
    print(f"laddered {n_q} questions ({n_q_ok} clean parses) -> {out_path}")
    print(f"report -> {report_path}")
    return 0


def _mean(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _ladder_order(mean_ranks: dict[str, float], eps: float = 0.10) -> list[list[str]]:
    """Group models into tiers by mean rank (lower = better). Models
    within ``eps`` of each other tie into one tier."""
    items = sorted(mean_ranks.items(), key=lambda kv: kv[1])
    tiers: list[list[str]] = []
    for m, r in items:
        if tiers and abs(r - mean_ranks[tiers[-1][0]]) <= eps:
            tiers[-1].append(m)
        else:
            tiers.append([m])
    return tiers


def render_ladder_report(ranks_by_model: dict, ranks_by_model_lang: dict,
                         meta: dict, pass_rate: dict, pq: dict, sq: dict) -> str:
    judge = meta["judge"]
    lines: list[str] = []
    lines.append(f"# Comparative ladder — judged by {judge}")
    lines.append("")
    lines.append(
        f"Instead of isolated 1–5 scores, **{judge}** was shown ALL models' "
        f"persisted code for each question at once and asked to rank them "
        f"best→worst (ties allowed). Model identities are anonymized to "
        f"letters and **shuffled per question** (seeded on `question_id`) so "
        f"the ranking can't lean on model names. Rank = fractional/average "
        f"rank (ties share the average position); **lower is better**."
    )
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- generated: `{meta['generated_at']}`  ·  git `{meta['git_commit']}`  ·  host `{meta['host']}`")
    lf = meta.get("lang_filter")
    lines.append(f"- judge: `{judge}` (num_predict={meta['num_predict']})  ·  languages: {', '.join(lf) if lf else 'all'}")
    lines.append(f"- questions laddered: {meta['n_questions']}  ·  clean parses: {meta['n_questions_valid']}")
    lines.append(f"- per-question rankings: `{meta['ladder_jsonl']}`")
    lines.append("")

    mean_ranks = {m: _mean(v) for m, v in ranks_by_model.items() if v}
    # Normalized: also report mean rank divided by participants would
    # need per-question N; the raw mean rank is comparable here since
    # every model appears in (nearly) every question of its languages.
    lines.append("## Overall ladder")
    lines.append("")
    if mean_ranks:
        order = _ladder_order(mean_ranks)
        rung = 1
        for tier in order:
            tie = " = ".join(f"`{m}`" for m in sorted(tier))
            mr = _mean([mean_ranks[m] for m in tier])
            lines.append(f"{rung}. {tie}  — mean rank {mr:.2f}")
            rung += len(tier)
    else:
        lines.append("_No valid rankings parsed._")
    lines.append("")

    # Detail + cross-check table
    lines.append("## Per-model detail & cross-check")
    lines.append("")
    lines.append("| model | mean rank | n_q | pass-rate | kimi q | gemini q |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for m in sorted(mean_ranks, key=lambda k: mean_ranks[k]):
        pr = pass_rate.get(m)
        lines.append(
            f"| `{m}` | {mean_ranks[m]:.2f} | {len(ranks_by_model[m])} | "
            f"{(pr*100):.0f}% | {_fmt(pq.get(m))} | {_fmt(sq.get(m))} |"
            if isinstance(pr, (int, float)) else
            f"| `{m}` | {mean_ranks[m]:.2f} | {len(ranks_by_model[m])} | — | "
            f"{_fmt(pq.get(m))} | {_fmt(sq.get(m))} |"
        )
    lines.append("")
    lines.append(
        "_Cross-check: a sound ladder broadly tracks pass-rate and the "
        "absolute quality columns; large inversions (top of the ladder with "
        "a low pass-rate) flag a judge that rewards style over correctness._"
    )
    lines.append("")

    # Per-language ladders
    lines.append("## Per-language ladders")
    lines.append("")
    for lang in sorted(ranks_by_model_lang):
        mr = {m: _mean(v) for m, v in ranks_by_model_lang[lang].items() if v}
        if not mr:
            continue
        lines.append(f"### {lang}")
        lines.append("")
        order = _ladder_order(mr)
        rung = 1
        for tier in order:
            tie = " = ".join(f"`{m}`" for m in sorted(tier))
            tmr = _mean([mr[m] for m in tier])
            lines.append(f"{rung}. {tie}  — mean rank {tmr:.2f}")
            rung += len(tier)
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ============================================================== #
# CLI
# ============================================================== #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rejudge",
        description="Offline second-judge re-score + comparative ladder for "
                    "the coder bench. Reads persisted sandboxes; no coder "
                    "re-run.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rescore", action="store_true",
                      help="Absolute second-judge re-score (writes "
                           "quality_secondary_* alongside the kimi fields).")
    mode.add_argument("--ladder", action="store_true",
                      help="Comparative ladder: one judge ranks all models "
                           "per question, best→worst with ties.")
    p.add_argument("--trials", required=True,
                   help="trials.jsonl file OR a results dir (merged "
                        "trials.jsonl or per-model */trials.jsonl).")
    p.add_argument("--questions-dir", default="",
                   help="Suite questions dir (for the task text + extension). "
                        "Required except for --report-only.")
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                   help=f"Second judge model. Default {DEFAULT_JUDGE_MODEL}.")
    p.add_argument("--ollama-base", default=DEFAULT_OLLAMA_BASE,
                   help=f"Ollama proxy base. Default {DEFAULT_OLLAMA_BASE}.")
    p.add_argument("--num-predict", type=int, default=DEFAULT_NUM_PREDICT,
                   help="Token budget for the judge (gemini reasoning model "
                        f"needs ≳400). Default {DEFAULT_NUM_PREDICT}.")
    p.add_argument("--timeout-s", type=float, default=120.0,
                   help="Per-judge-call timeout seconds. Default 120.")
    p.add_argument("--concurrency", type=int, default=1,
                   help="Parallel judge calls (thread pool, one client "
                        "each). 1 = sequential. Keep modest (4–8) — all "
                        "workers share the Ollama-Pro endpoint. Default 1.")
    p.add_argument("--report-only", action="store_true",
                   help="(--rescore) Re-render the report from an existing "
                        "trials-rejudged.jsonl (pass it as --trials). No "
                        "judging, no cloud spend.")
    p.add_argument("--retry-unparseable", action="store_true",
                   help="Re-judge ONLY the items a prior run failed to parse "
                        "(gemini reasoning truncations) — pass the prior "
                        "rejudged jsonl (--rescore) or trials jsonl with the "
                        "ladder.jsonl in place (--ladder), usually with a "
                        "higher --num-predict. Recovered results merge into "
                        "the existing output; valid prior results are kept.")
    p.add_argument("--lang", default="",
                   help="Comma-separated language filter (e.g. "
                        "c,cpp,csharp,python). Default all.")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap rescored trials (sanity probe). 0 = no cap. "
                        "Ignored by --ladder.")
    p.add_argument("--output", default="",
                   help="Output jsonl path. Default trials-rejudged.jsonl / "
                        "ladder.jsonl next to the input.")
    p.add_argument("--report", default="",
                   help="Markdown report path. Default rescore-report.md / "
                        "ladder-report.md next to the output.")
    return p


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    args = build_parser().parse_args(argv)
    if args.rescore:
        return cmd_rescore(args)
    return cmd_ladder(args)


if __name__ == "__main__":
    raise SystemExit(main())
