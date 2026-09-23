#!/usr/bin/env python3
"""Is a cheaper model as good a coder_bench judge as the reference?

Offline, like ``rejudge.py``: it reads the solutions a ``coder_bench``
run left on disk and asks a candidate judge to score them with the
exact prompt the bench's own judge used (``build_judge_messages``, no
``num_predict``). No coder is re-run.

"Agrees with the reference" is not enough on its own — it would only
copy the reference's biases — so the report measures the judge against
things that do not depend on any judge:

- **Oracle separation.** The bench's tests say which solutions pass.
  A useful judge scores passing code above failing code; the AUC of
  score vs ``passes_tests`` is computed for candidate and reference
  alike.
- **Self-bias.** The candidate's offset from the reference on its own
  family's code, minus its offset on everyone else's.
- **Style affinity.** The failure glm-5.2 showed: scoring other models
  higher the more their code looked like its own. When the candidate
  is also a subject of the run, each solution's token similarity to
  the candidate's own solution for that question is set against the
  candidate's residual (candidate − reference).
- **Style invariance.** Passing solutions are re-scored after a change
  that leaves the logic alone — comments stripped, layout changed. The
  score should not move; the reference is run on the same variants so
  "should not" is measured against a judge we trust.
- **Stability.** A sample is judged twice.
- **Speed and dollars**, per verdict, from ``harness.timed_chat``.

Verdicts append to ``<out>/verdicts.jsonl`` one per line as they land,
and a re-run skips every key already there, so a run can stop at a
session limit and resume.

    judge_eval.py judge --judge glm-5.3-flash:cloud \\
        --trials results/2026-09-23/coder_med \\
        --questions-dir questions/coder_med \\
        --out results/2026-09-23/judge_eval --kinds base,retest,variant
    judge_eval.py report --out results/2026-09-23/judge_eval \\
        --judge glm-5.3-flash:cloud --second-pass deepseek-v4.1-flash:cloud
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import logging
import re
import statistics
import sys
import threading
import tokenize
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import (  # noqa: E402
    build_judge_messages,
    judge_lang_for_path,
    parse_judge_response,
    timed_chat,
)
from benchmarks.consultants.pricing import model_key, usage_cost  # noqa: E402
from claude_hooks import model_sampling  # noqa: E402
from benchmarks.consultants.rejudge import (  # noqa: E402
    _question_map,
    _resolve_source,
    load_raw_trials,
)

log = logging.getLogger("benchmarks.consultants.judge_eval")

REFERENCE_JUDGE = "kimi-k2.6:cloud"
VARIANTS = ("no_comments", "relayout")
_C_LIKE = {"c", "cpp", "csharp", "go", "rust"}


# ============================================================== #
# Style variants: change the look, never the logic
# ============================================================== #

def _lang(question_id: str) -> str:
    return question_id.split("-", 1)[0]


_CHAR_LIT = re.compile(r"'(?:\\.|[^\\'\n]){1,8}'")


def _strip_c_comments(code: str) -> Optional[str]:
    """Drop ``//`` and ``/* */`` comments, leaving string and char
    literals alone. Rust lifetimes (``'a``) are not char literals, so a
    quote only opens one when a whole literal follows. Raw / verbatim
    strings are not parsed; code that has them is skipped."""
    if 'R"(' in code or 'r#"' in code or '@"' in code:
        return None
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
        elif code.startswith("/*", i):
            j = code.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
        elif ch == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            out.append(code[i:j + 1])
            i = j + 1
        elif ch == "'" and (m := _CHAR_LIT.match(code, i)):
            out.append(m.group(0))
            i = m.end()
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _strip_py_comments(code: str) -> Optional[str]:
    """Drop ``#`` comments by token position. Docstrings stay: they are
    part of the program, not a comment on it."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    lines = code.splitlines(keepends=True)
    for tok in reversed(toks):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            line = lines[row - 1]
            nl = "\n" if line.endswith("\n") else ""
            lines[row - 1] = line[:col].rstrip() + nl
    return "".join(lines)


def strip_comments(code: str, lang: str) -> Optional[str]:
    """``code`` without comments, or None when it cannot be done safely.
    Lines that held only a comment are dropped; blank lines that were
    already blank stay."""
    before = code.splitlines()
    stripped = (_strip_py_comments(code) if lang == "python"
                else _strip_c_comments(code) if lang in _C_LIKE else None)
    if stripped is None:
        return None
    after = stripped.splitlines()
    if len(after) != len(before):  # a block comment spanned lines
        kept = [ln for ln in after if ln.strip()]
    else:
        kept = [a for a, b in zip(after, before) if a.strip() or not b.strip()]
    return "\n".join(ln.rstrip() for ln in kept) + "\n"


def relayout(code: str, lang: str) -> Optional[str]:
    """Same tokens, different layout: blank lines removed, and in the
    C-like languages other than Go an opening brace moved to its own
    line (Go's semicolon insertion makes that a syntax error there).
    Python code with triple-quoted strings is skipped, since a blank
    line inside one is content."""
    if lang == "python" and ('"""' in code or "'''" in code):
        return None
    if lang not in _C_LIKE and lang != "python":
        return None
    out: list[str] = []
    for line in code.splitlines():
        if not line.strip():
            continue
        s = line.rstrip()
        if (lang in _C_LIKE and lang != "go" and s.endswith("{")
                and s.strip() != "{" and "//" not in s and '"' not in s):
            indent = s[:len(s) - len(s.lstrip())]
            out.append(s[:-1].rstrip())
            out.append(indent + "{")
        else:
            out.append(s)
    return "\n".join(out) + "\n"


def make_variant(code: str, lang: str, variant: str) -> Optional[str]:
    fn = {"no_comments": strip_comments, "relayout": relayout}[variant]
    v = fn(code, lang)
    return None if v is None or v.strip() == code.strip() else v


# ============================================================== #
# Work selection
# ============================================================== #

def _h(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def verdict_key(kind: str, variant: str, qid: str, subject: str,
                judge: str, rep: int = 0) -> str:
    return "|".join((kind, variant, qid, subject, judge, str(rep)))


def build_work(rows: list[dict], qmap: dict, *, judge: str, kinds: set,
               models: Optional[set], retest_n: int, variant_n: int,
               options: Optional[dict] = None, retest_repeats: int = 1
               ) -> list[dict]:
    """Every verdict the requested kinds call for, as dicts carrying
    the messages to send. Samples are chosen by hash, so every judge
    gets the same retest and variant sample."""
    usable = []
    for r in rows:
        if models and r.get("model") not in models:
            continue
        q = qmap.get(r.get("question_id", ""))
        code, _ = _resolve_source(r)
        if q is None or code is None:
            continue
        usable.append((r, q, code))
    usable.sort(key=lambda t: _h(t[0]["question_id"], t[0]["model"]))
    options = options or {}
    # The judge's name in every key and record carries its sampling, so
    # verdicts under different settings never collide or pool.
    judge_label = model_sampling.label(judge, options)
    work = []

    def add(kind, variant, r, q, code, rep=0):
        language, fence = judge_lang_for_path(q.sandbox_path)
        work.append({
            "key": verdict_key(kind, variant, r["question_id"], r["model"],
                               judge_label, rep),
            "kind": kind, "variant": variant, "rep": rep,
            "question_id": r["question_id"], "subject": r["model"],
            "judge": judge_label, "judge_model": judge, "options": options,
            "messages": build_judge_messages(q.task, code, language=language,
                                             fence=fence),
        })

    if "base" in kinds:
        for r, q, code in usable:
            add("base", "", r, q, code)
    if "retest" in kinds:
        for r, q, code in usable[:retest_n]:
            for rep_i in range(1, max(1, retest_repeats) + 1):
                add("retest", "", r, q, code, rep=rep_i)
    if "variant" in kinds:
        passing = [t for t in usable if t[0].get("passes_tests")]
        for r, q, code in passing[:variant_n]:
            for v in VARIANTS:
                vc = make_variant(code, _lang(r["question_id"]), v)
                if vc is not None:
                    add("variant", v, r, q, vc)
    return work


# ============================================================== #
# Judging
# ============================================================== #

def _load_verdicts(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line cut by a kill; its key will be redone
    return out


def _make_client(model: str, base: str, timeout_s: float):
    from benchmarks.consultants.harness import bench_client as make_agent_chat_client
    # One transport retry: a retried call is paid for again, and a
    # judge that needs several is a finding, not noise to hide.
    return make_agent_chat_client(model, base, timeout_s=timeout_s,
                                  max_retries=1)


def _judge_one(client, item: dict) -> dict:
    usage: dict = {}
    text, err = "", ""
    try:
        payload = {"model": item["judge_model"], "messages": item["messages"],
                   "stream": False}
        if item.get("wire_options"):
            payload["options"] = dict(item["wire_options"])
        resp = timed_chat(client, payload, usage, "judge", item["judge_model"])
        choices = (resp or {}).get("choices") or [{}]
        text = ((choices[0] or {}).get("message") or {}).get("content") or ""
    except Exception as e:  # noqa: BLE001 — a failed verdict is data
        err = f"{type(e).__name__}: {e}"
    score, rationale = parse_judge_response(text) if text else (None, "")
    rec = {k: v for k, v in item.items() if k not in ("messages", "wire_options")}
    rec.update(score=score, rationale=rationale[:300], error=err or None,
               usage=usage, at=datetime.now(timezone.utc).isoformat())
    return rec


def cmd_judge(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    vpath = out / "verdicts.jsonl"
    done = {v["key"] for v in _load_verdicts(vpath)
            if v.get("score") is not None or not args.retry_failed}
    rows = load_raw_trials(Path(args.trials))
    qmap = _question_map(Path(args.questions_dir))
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    models = ({m.strip() for m in args.models.split(",") if m.strip()}
              if args.models else None)
    template = model_sampling.sampling_for(args.judge)
    options = dict(template) if args.sampling == "template" else {}
    options.update(model_sampling.parse_options(args.option or []))
    if args.temperature is not None:
        options["temperature"] = args.temperature
    # What goes on the wire: a template field this run does not set must
    # be cancelled explicitly, or the chat client would add it back.
    wire = {**{k: None for k in template}, **options}
    work = [w for w in build_work(rows, qmap, judge=args.judge, kinds=kinds,
                                  models=models, retest_n=args.retest_n,
                                  variant_n=args.variant_n, options=options,
                                  retest_repeats=args.retest_repeats)
            if w["key"] not in done]
    for w in work:
        w["wire_options"] = wire
    log.info("%d verdicts to get from %s (%d already on disk)",
             len(work), model_sampling.label(args.judge, options), len(done))
    lock = threading.Lock()
    tl = threading.local()

    def run(item):
        if getattr(tl, "client", None) is None:
            tl.client = _make_client(args.judge, args.ollama_base,
                                     args.timeout_s)
        rec = _judge_one(tl.client, item)
        with lock, open(vpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        log.info("%s %s × %s -> %s %s", rec["kind"], rec["question_id"],
                 rec["subject"], rec["score"], rec["error"] or "")

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(run, work))
    return 0


def cmd_synth(args) -> int:
    """Settle verdicts the panel members already gave on the same
    solution with the synthesizer — ``judge_panel.resolve``, the same
    code coder_bench runs live — and file them under the panel label, so
    ``report --judge <label>`` grades the panel like any judge."""
    from benchmarks.consultants import judge_panel
    out = Path(args.out)
    vpath = out / "verdicts.jsonl"
    verdicts = _load_verdicts(vpath)
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    plabel = judge_panel.label(members, args.synth)
    # Settled = has a score. A panel verdict with none (both members had
    # failed) is a hole a repair pass settles again.
    done = {v["key"] for v in verdicts
            if v["judge"] == plabel and v.get("score") is not None}
    by = {}
    for v in verdicts:
        if v["judge"] in members:
            by[(v["judge"], v["kind"], v["variant"], v["question_id"],
                v["subject"], v.get("rep", 0))] = v
    rows = load_raw_trials(Path(args.trials))
    qmap = _question_map(Path(args.questions_dir))
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    work = []
    for r in rows:
        q = qmap.get(r.get("question_id", ""))
        code, _ = _resolve_source(r)
        if q is None or code is None:
            continue
        for (j, kind, variant, qid, subj, rep) in list(by):
            if (j != members[0] or kind not in kinds or variant
                    or qid != r["question_id"] or subj != r["model"]):
                continue
            mv = [by.get((m, kind, variant, qid, subj, rep)) for m in members]
            if None in mv:
                continue
            key = verdict_key(kind, variant, qid, subj, plabel, rep)
            if key not in done:
                work.append({"key": key, "kind": kind, "variant": variant,
                             "rep": rep, "question_id": qid, "subject": subj,
                             "judge": plabel, "judge_model": args.synth,
                             "task": q.task, "code": code,
                             "path": q.sandbox_path, "members": mv})
    log.info("%d panel verdicts to settle (%d on disk)", len(work), len(done))
    lock = threading.Lock()
    tl = threading.local()

    def run(item):
        if getattr(tl, "client", None) is None:
            tl.client = _make_client(args.synth, args.ollama_base, args.timeout_s)
        usage: dict = {}
        for i, v in enumerate(item["members"]):
            for u in (v.get("usage") or {}).values():
                usage[f"judge_{chr(97 + i)}"] = dict(u)

        def call(msgs):
            resp = timed_chat(tl.client, {"model": args.synth, "messages": msgs,
                                          "stream": False},
                              usage, "judge_synth", args.synth)
            choices = (resp or {}).get("choices") or [{}]
            return ((choices[0] or {}).get("message") or {}).get("content") or ""
        language, fence = judge_lang_for_path(item["path"])
        res = judge_panel.resolve(
            [(v["judge"], v["score"], v.get("rationale") or "")
             for v in item["members"]],
            key=f"{item['question_id']}|{item['subject']}", synth=call,
            task=item["task"], code=item["code"], language=language,
            fence=fence)
        rec = {k: v for k, v in item.items()
               if k not in ("task", "code", "path", "members")}
        rec.update(score=res["score"], rationale=res["rationale"],
                   source=res["source"], discordant=res["discordant"],
                   claims=res["claims"], error=res["synth_error"],
                   member_scores=[v["score"] for v in item["members"]],
                   usage=usage, at=datetime.now(timezone.utc).isoformat())
        with lock, open(vpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        log.info("%s %s × %s %s -> %s (%s)", rec["kind"], rec["question_id"],
                 rec["subject"], rec["member_scores"], rec["score"],
                 rec["source"])

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(run, work))
    return 0


# ============================================================== #
# Statistics (stdlib only)
# ============================================================== #

def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    return pearson(_ranks(xs), _ranks(ys))


def auc(scores: list[float], labels: list[bool]) -> Optional[float]:
    """P(a passing solution outscores a failing one), ties count half."""
    pos = [s for s, lab in zip(scores, labels) if lab]
    neg = [s for s, lab in zip(scores, labels) if not lab]
    if not pos or not neg:
        return None
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def family(model: str) -> str:
    # A panel label is filed under its synthesizer (judge_panel.label).
    model = model_sampling.model_of(model.split("#", 1)[0])
    key = model_key(model) or model
    return re.split(r"[-:.]", key, maxsplit=1)[0]


def similarity(a: str, b: str) -> float:
    ta = re.findall(r"\w+|[^\w\s]", a)
    tb = re.findall(r"\w+|[^\w\s]", b)
    return difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()


# ============================================================== #
# Report
# ============================================================== #

def _f(x, nd=2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def _verdict_cost(v: dict) -> Optional[float]:
    at = datetime.fromisoformat(v["at"]) if v.get("at") else None
    c = usage_cost(v.get("usage") or {}, at)
    return None if c["unpriced"] else c["total"]


def _speed_cost(vs: list[dict]) -> dict:
    walls = sorted(u["wall_s"] for v in vs for u in (v.get("usage") or {}).values()
                   if "wall_s" in u)
    costs = [c for c in map(_verdict_cost, vs) if c is not None]
    return {
        "n": len(vs),
        "failed": sum(1 for v in vs if v.get("score") is None),
        "median_s": statistics.median(walls) if walls else None,
        "p90_s": walls[int(0.9 * (len(walls) - 1))] if walls else None,
        "usd_per_verdict": _mean(costs),
    }


def render_report(rows: list[dict], verdicts: list[dict], *, judge: str,
                  reference: str, second_pass: Optional[str]) -> str:
    trial = {(r["question_id"], r["model"]): r for r in rows}
    ref_base = {k: r.get("quality_score") for k, r in trial.items()
                if isinstance(r.get("quality_score"), (int, float))}

    def mine(j, kind, variant=""):
        return {(v["question_id"], v["subject"]): v for v in verdicts
                if v["judge"] == j and v["kind"] == kind
                and v["variant"] == variant}

    jb = mine(judge, "base")
    L = [f"# Judge evaluation — `{judge}` against `{reference}`", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
         f"{len(rows)} trials, {len(jb)} base verdicts by the candidate.", ""]

    # --- agreement + oracle separation
    both = [k for k in jb if jb[k]["score"] is not None and k in ref_base]
    js = [jb[k]["score"] for k in both]
    rs = [ref_base[k] for k in both]
    passes = [bool(trial[k].get("passes_tests")) for k in both]
    diffs = [a - b for a, b in zip(js, rs)]
    L += ["## Grading quality", "",
          "| measure | candidate | reference |", "|---|---|---|",
          f"| paired verdicts | {len(both)} | {len(both)} |",
          f"| mean score | {_f(_mean(js))} | {_f(_mean(rs))} |",
          f"| **AUC, score vs tests passing** | **{_f(auc(js, passes))}** "
          f"| **{_f(auc(rs, passes))}** |", "",
          f"Agreement: Spearman ρ {_f(spearman(js, rs))}, mean |Δ| "
          f"{_f(_mean([abs(d) for d in diffs]))}, exact "
          f"{_f(100 * _mean([d == 0 for d in diffs]) if diffs else None, 0)}%, "
          f"within one point "
          f"{_f(100 * _mean([abs(d) <= 1 for d in diffs]) if diffs else None, 0)}%.",
          ""]

    # --- per subject + self-bias
    fam = family(judge)
    L += ["## By subject (candidate − reference)", "",
          "| subject | n | candidate | reference | offset |", "|---|---|---|---|---|"]
    own, other = [], []
    for subj in sorted({k[1] for k in both}):
        ks = [k for k in both if k[1] == subj]
        off = _mean([jb[k]["score"] - ref_base[k] for k in ks])
        (own if family(subj) == fam else other).extend(
            jb[k]["score"] - ref_base[k] for k in ks)
        L.append(f"| {subj}{' *(own family)*' if family(subj) == fam else ''} "
                 f"| {len(ks)} | {_f(_mean([jb[k]['score'] for k in ks]))} "
                 f"| {_f(_mean([ref_base[k] for k in ks]))} | {_f(off)} |")
    self_bias = (_mean(own) - _mean(other)) if own and other else None
    L += ["", f"**Self-bias** (offset on own family − offset on others): "
          f"**{_f(self_bias)}** points.", ""]

    # --- style affinity
    own_code = {}
    for (qid, subj), r in trial.items():
        if subj == judge:
            code, _ = _resolve_source(r)
            if code:
                own_code[qid] = code
    aff_x, aff_y = [], []
    for k in both:
        if family(k[1]) == fam or k[0] not in own_code:
            continue
        code, _ = _resolve_source(trial[k])
        if code:
            aff_x.append(similarity(code, own_code[k[0]]))
            aff_y.append(jb[k]["score"] - ref_base[k])
    L += ["## Style affinity", ""]
    if len(aff_x) >= 10:
        order = sorted(range(len(aff_x)), key=lambda i: aff_x[i])
        third = len(order) // 3
        buckets = [order[:third], order[third:2 * third], order[2 * third:]]
        L += [f"Other models' code, by token similarity to the candidate's "
              f"own solution to the same question (n={len(aff_x)}). A judge "
              "that rewards its own style shows a residual that rises "
              "with similarity.", "",
              "| similarity tercile | mean similarity | mean residual |",
              "|---|---|---|"]
        for name, b in zip(("least like its own", "middle", "most like its own"),
                           buckets):
            L.append(f"| {name} | {_f(_mean([aff_x[i] for i in b]))} "
                     f"| {_f(_mean([aff_y[i] for i in b]))} |")
        L += ["", f"Pearson r(similarity, residual) = "
              f"**{_f(pearson(aff_x, aff_y))}**.", ""]
    else:
        L += ["Not measurable: the candidate is not a subject of this run, "
              "or too few paired verdicts.", ""]

    # --- style invariance
    L += ["## Style invariance", "",
          "Score change when only the look of a passing solution changes.", "",
          "| variant | judge | n | mean Δ | mean abs Δ |", "|---|---|---|---|---|"]
    ref_var_base = mine(reference, "base") or {k: {"score": s}
                                              for k, s in ref_base.items()}
    for v in VARIANTS:
        for j, base in ((judge, jb), (reference, ref_var_base)):
            var = mine(j, "variant", v)
            ds = [var[k]["score"] - base[k]["score"] for k in var
                  if var[k]["score"] is not None and k in base
                  and base[k].get("score") is not None]
            L.append(f"| {v} | {j} | {len(ds)} | {_f(_mean(ds))} "
                     f"| {_f(_mean([abs(d) for d in ds]))} |")
    L.append("")

    # --- stability
    rt = mine(judge, "retest")
    pairs = [(jb[k]["score"], rt[k]["score"]) for k in rt
             if k in jb and None not in (jb[k]["score"], rt[k]["score"])]
    L += ["## Stability", "",
          f"{len(pairs)} solutions judged twice by the candidate: exact "
          f"{_f(100 * _mean([a == b for a, b in pairs]) if pairs else None, 0)}%, "
          f"within one point "
          f"{_f(100 * _mean([abs(a - b) <= 1 for a, b in pairs]) if pairs else None, 0)}%.",
          ""]

    # --- second pass
    if second_pass:
        sp = mine(second_pass, "base")
        ks = [k for k in sp if sp[k]["score"] is not None and k in ref_base
              and family(k[1]) == fam]
        a = [sp[k]["score"] for k in ks]
        b = [ref_base[k] for k in ks]
        c = [jb[k]["score"] for k in ks if k in jb and jb[k]["score"] is not None]
        L += [f"## Second pass on {fam} code: `{second_pass}`", "",
              f"{len(ks)} {fam}-family solutions. Mean score: second pass "
              f"{_f(_mean(a))}, reference {_f(_mean(b))}, candidate "
              f"{_f(_mean(c))}. Second pass vs reference: offset "
              f"{_f(_mean([x - y for x, y in zip(a, b)]))}, Spearman "
              f"{_f(spearman(a, b))}, AUC "
              f"{_f(auc(a, [bool(trial[k].get('passes_tests')) for k in ks]))}.",
              ""]

    # --- speed + cost
    L += ["## Speed and cost per verdict", "",
          "| judge | verdicts | failed | median s | p90 s | $ / verdict |",
          "|---|---|---|---|---|---|"]
    for j in sorted({v["judge"] for v in verdicts}):
        s = _speed_cost([v for v in verdicts if v["judge"] == j])
        L.append(f"| {j} | {s['n']} | {s['failed']} | {_f(s['median_s'], 1)} "
                 f"| {_f(s['p90_s'], 1)} | {_f(s['usd_per_verdict'], 4)} |")
    L += ["", "The reference's own base verdicts were made by coder_bench "
          "before per-call timing existed; its speed here comes from the "
          "verdicts this tool asked it for.", ""]
    return "\n".join(L)


def cmd_report(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = load_raw_trials(Path(args.trials))
    verdicts = _load_verdicts(out / "verdicts.jsonl")
    text = render_report(rows, verdicts, judge=args.judge,
                         reference=args.reference,
                         second_pass=args.second_pass or None)
    (out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judge", help="collect verdicts (resumable)")
    j.add_argument("--judge", required=True)
    j.add_argument("--trials", required=True)
    j.add_argument("--questions-dir", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--kinds", default="base,retest,variant")
    j.add_argument("--models", default="",
                   help="only these subjects (comma list)")
    j.add_argument("--retest-n", type=int, default=20)
    j.add_argument("--variant-n", type=int, default=40)
    j.add_argument("--ollama-base", default="http://192.168.178.161:11434")
    j.add_argument("--timeout-s", type=float, default=300.0)
    j.add_argument("--concurrency", type=int, default=1,
                   help="≤2: Ollama Pro allows 3 connections and the "
                        "hooks hold one")
    j.add_argument("--sampling", choices=("template", "none"),
                   default="template",
                   help="template: send the model's sampling template "
                        "(claude_hooks.model_sampling); none: provider default")
    j.add_argument("--option", action="append", metavar="FIELD=VALUE",
                   help="a sampler option over the --sampling base, e.g. "
                        "repeat_penalty=1.1 (repeatable; any field in "
                        "claude_hooks.model_sampling.FIELDS)")
    j.add_argument("--temperature", type=float, default=None,
                   help="shorthand for --option temperature=T")
    j.add_argument("--retest-repeats", type=int, default=1,
                   help="judge each retest solution this many extra times")
    j.add_argument("--retry-failed", action="store_true",
                   help="also redo verdicts that returned no score")
    j.set_defaults(fn=cmd_judge)
    y = sub.add_parser("synth", help="settle member verdicts with the "
                                     "panel synthesizer (resumable)")
    y.add_argument("--members", required=True,
                   help="comma list of member judge labels, panel order")
    y.add_argument("--synth", default="deepseek-v4.1-flash:cloud")
    y.add_argument("--trials", required=True)
    y.add_argument("--out", required=True)
    y.add_argument("--questions-dir", required=True)
    y.add_argument("--kinds", default="base,retest")
    y.add_argument("--ollama-base", default="http://192.168.178.161:11434")
    y.add_argument("--timeout-s", type=float, default=300.0)
    y.add_argument("--concurrency", type=int, default=1)
    y.set_defaults(fn=cmd_synth)
    r = sub.add_parser("report", help="render report.md from verdicts")
    r.add_argument("--out", required=True)
    r.add_argument("--trials", required=True)
    r.add_argument("--judge", required=True)
    r.add_argument("--reference", default=REFERENCE_JUDGE)
    r.add_argument("--second-pass", default="")
    r.set_defaults(fn=cmd_report)
    return p


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
