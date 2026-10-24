"""Mine decision-model bench cases from real councils (plan H6).

The H4 cases (``questions/decision/cases.json``) are hand-written and
cleaner than council output. This walks every ``transcript.db`` under
``--roots`` and builds cases in the same shape, with the question and
state each live seam sends (``consultants/engine/decision.py``), every
field cut by ``decision_client.bound_text`` with the seam's own limit:

* ``struggle`` — a researcher lane's recent calls, rendered as
  ``council._struggle_watch`` renders them, at every
  ``STRUGGLE_CHECK_EVERY``-th result. The label comes from the lane
  itself: *stuck* when most of the shown calls and nearly all of the next
  six brought nothing new (a repeated call, a repeated output or an
  error); not stuck when nearly all of the next calls were new —
  *recovered* when two or more of the shown six were stale (the hard
  negatives: a nudge there was not needed), *progressing* otherwise.
  Windows in between are left out — the label is never guessed. Real
  lanes are almost never stuck by this rule, so the seam's false-alarm
  rate is what real cases can measure.
* ``citations`` — each ``path:line`` cite in a researcher report or final
  answer, with the claim and code ``citation_linter.semantic_items``
  would send, the code read from the repo at the commit the council saw.
  A cite whose line lacks every identifier the claim names, while the
  file has one elsewhere, is a ``wrong-line`` candidate; one whose window
  holds a named identifier is a ``names-it`` candidate. Both are
  *candidates*: naming a symbol is not supporting a claim, so each needs
  a reading before it is a case (``label_source: "pending"``).
* ``review`` — a parent council's final answer with the follow-up the
  reviewer asked next, for a reading of whether that follow-up says the
  answer was wrong or incomplete (``pending``); and the last answer of
  every consultancy the reviewer marked ``accepted`` (``truth: true``,
  ``label_source: "accepted"`` — no reading needed). Each carries
  ``live``: the scores the ``confidence`` / ``review`` seams recorded on
  that answer at the time (``decision`` events, shadow or acting), so a
  per-server mapping can be read off live scores without re-asking.

Output (git-ignored; councils from other repos carry their code):
``results/<date>/decision-h6/candidates.json``. After the readings are
written to ``labels.json`` beside it, ``--assemble`` builds the bench's
``cases.json``.

    python benchmarks/consultants/mine_decision_cases.py \\
        --roots /srv/dev-disk-by-label-opt/dev
    python benchmarks/consultants/mine_decision_cases.py --assemble
    python benchmarks/consultants/decision_bench.py \\
        --cases results/<date>/decision-h6/cases.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]

from consultants.engine.citation_linter import (  # noqa: E402
    CITATION_RE, SEMANTIC_CLAIM_CHARS, SEMANTIC_CONTEXT_LINES,
    _already_annotated_at, _is_url_tail)
from consultants.engine.council import STRUGGLE_CHECK_EVERY  # noqa: E402
from claude_hooks.decision_client import bound_text  # noqa: E402

#: What the struggle seam's question asks of the window.
STRUGGLE_Q = ("Is the agent stuck, repeating itself without new "
              "information, rather than making progress toward `goal`?")
CITATION_Q = "Does `c0.code` support `c0.claim`?"
REVIEW_Q = ("Does `answer` correctly and completely answer `question`, "
            "with evidence for its conclusions?")

_ERROR_HEAD = re.compile(r"^\s*(error|traceback|\{\s*\"success\"\s*:\s*false)",
                         re.I)
_IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)(?:\(\))?`")


# --- reading councils ------------------------------------------------------ #

def transcripts(roots: list[str], depth: int = 6) -> list[Path]:
    out: set[str] = set()
    for root in roots:
        for d in range(0, depth):
            pat = root + "/*" * d + "/.claude-hooks/consultants/*/transcript.db"
            out |= {os.path.realpath(p) for p in glob.glob(pat)}
    return sorted(Path(p) for p in out)


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _meta(con) -> dict:
    try:
        row = con.execute("SELECT sid, cwd, question, started_at, parent_sid "
                          "FROM meta LIMIT 1").fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return dict(zip(("sid", "cwd", "question", "started_at", "parent_sid"), row))


def _content(response_json: Optional[str]) -> str:
    try:
        msg = json.loads(response_json or "{}")["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return str(msg.get("content") or "")


def _plan(con) -> str:
    rows = con.execute("SELECT response_json FROM events WHERE kind='llm_call' "
                       "AND role='planner' ORDER BY ts").fetchall()
    for (r,) in reversed(rows):
        text = _content(r)
        if text.strip():
            return text
    return ""


# --- struggle -------------------------------------------------------------- #

def _render_call(tool: str, args: str, output: str) -> str:
    """As ``council._struggle_watch._calls`` renders one result."""
    head = str(output or "").strip().splitlines()
    return (f"{tool} {str(args or '')[:160]} -> "
            f"{(head[0] if head else '(empty)')[:120]}")


def _novelty(calls: list[tuple]) -> list[bool]:
    """Per call: did it bring something new to the lane — a call not made
    before, that did not fail, whose output was not seen before."""
    sigs: set = set()
    outs: set = set()
    new: list[bool] = []
    for tool, args, output, error in calls:
        try:
            canon = json.dumps(json.loads(args or "{}"), sort_keys=True)
        except ValueError:
            canon = str(args)
        sig = (tool, canon)
        digest = hashlib.sha1(str(output or "").encode()).hexdigest()
        failed = bool(error) or bool(_ERROR_HEAD.match(str(output or "")))
        new.append(sig not in sigs and not failed and digest not in outs)
        sigs.add(sig)
        outs.add(digest)
    return new


def struggle_cases(con, meta: dict, goal: str) -> list[dict]:
    rows = con.execute(
        "SELECT round, lane_idx, tool, args, output, error FROM events "
        "WHERE kind='tool_call' AND role='researcher' ORDER BY ts").fetchall()
    lanes: dict = {}
    for rnd, lane, tool, args, output, error in rows:
        lanes.setdefault((rnd, lane), []).append((tool, args, output, error))
    out = []
    for (rnd, lane), calls in lanes.items():
        new = _novelty(calls)
        for k in range(STRUGGLE_CHECK_EVERY, len(calls),
                       STRUGGLE_CHECK_EVERY):
            future = new[k:k + 6]
            if len(future) < 4:
                continue
            shown_stale = sum(1 for x in new[k - 6:k] if not x)
            fut_new = sum(future)
            if shown_stale >= 4 and fut_new <= 1:
                truth, label = True, "stuck"
            elif fut_new >= len(future) - 1:
                # Stale-looking windows the lane recovered from on its own
                # are the hard negatives: a nudge there was not needed.
                truth = False
                label = "recovered" if shown_stale >= 2 else "progressing"
            else:
                continue
            shown = [_render_call(t, a, o) for t, a, o, _ in calls[:k]][-12:]
            out.append({
                "seam": "struggle", "kind": "noul", "truth": truth,
                "label_source": "lane-future", "candidate": label,
                "evidence": {"shown_stale_of_6": shown_stale,
                             "next_new": fut_new, "next_n": len(future)},
                "origin": {"sid": meta["sid"], "round": rnd, "lane": lane,
                           "at_call": k},
                "state": {"goal": bound_text(goal, 2_000),
                          "recent_calls": [bound_text(x, 400) for x in shown]},
                "instructions": STRUGGLE_Q})
    return out


# --- citations ------------------------------------------------------------- #

def _rev_at(repo: str, ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    try:
        rev = subprocess.run(
            ["git", "-C", repo, "rev-list", "-1", f"--before=@{int(ts)}",
             "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return rev or None


_FILE_CACHE: dict = {}


def _file_at(repo: str, rev: str, path: str) -> Optional[list[str]]:
    key = (repo, rev, path)
    if key not in _FILE_CACHE:
        try:
            r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"],
                               capture_output=True, timeout=20)
            _FILE_CACHE[key] = (r.stdout.decode(errors="replace").splitlines()
                                if r.returncode == 0 else None)
        except (OSError, subprocess.SubprocessError):
            _FILE_CACHE[key] = None
    return _FILE_CACHE[key]


def citation_cases(texts: list[str], meta: dict) -> list[dict]:
    repo = meta.get("cwd") or ""
    rev = _rev_at(repo, meta.get("started_at"))
    if not rev:
        return []
    out, seen = [], set()
    for text in texts:
        for m in CITATION_RE.finditer(text or ""):
            if _is_url_tail(text, m.start()) or _already_annotated_at(text, m.end()):
                continue
            full = m.group(0)
            if full in seen:
                continue
            seen.add(full)
            path = m.group("path").lstrip("./")
            if os.path.isabs(path):
                try:
                    path = os.path.relpath(path, repo)
                except ValueError:
                    continue
            lines = _file_at(repo, rev, path)
            start = int(m.group("line_start"))
            end = int(m.group("line_end") or start)
            if not lines or start < 1 or start > len(lines):
                continue
            lo = max(1, start - SEMANTIC_CONTEXT_LINES)
            hi = min(len(lines), max(start, end) + SEMANTIC_CONTEXT_LINES)
            code = "\n".join(f"{n}: {lines[n - 1]}" for n in range(lo, hi + 1))
            head = text[max(0, m.start() - SEMANTIC_CLAIM_CHARS):m.end()]
            cut = max(head.rfind("\n"), head.rfind(". ", 0, len(head) - len(full)))
            claim = head[cut + 1:].strip() if cut >= 0 else head.strip()
            names = {n.split(".")[-1] for n in _IDENT.findall(claim)
                     if n.split(".")[-1] and "/" not in n
                     and not n.endswith((".py", ".ts", ".md"))}
            names = {n for n in names if len(n) >= 4}
            if not names:
                continue
            window = "\n".join(lines[lo - 1:hi])
            whole = "\n".join(lines)
            in_window = {n for n in names if n in window}
            if in_window:
                kind = "names-it"
            elif any(n in whole for n in names):
                kind = "wrong-line"
            else:
                continue
            out.append({
                "seam": "citations", "kind": "noul", "truth": None,
                "label_source": "pending", "candidate": kind,
                "origin": {"sid": meta["sid"], "cite": full, "rev": rev[:12],
                           "names": sorted(names)},
                "state": {"c0": {"claim": bound_text(claim, 600),
                                 "code": bound_text(code, 2_000)}},
                "instructions": CITATION_Q})
    return out


# --- review ---------------------------------------------------------------- #

def live_scores(con) -> list[dict]:
    """The synthesizer's recorded ``decision`` events: which server and
    model answered, ``sound`` (confidence) and ``next`` (review), and
    whether the seams were in shadow."""
    try:
        rows = con.execute("SELECT payload FROM runtime_events WHERE "
                           "kind='decision' AND role='synthesizer' "
                           "ORDER BY ts").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for (payload,) in rows:
        try:
            body = json.loads(payload or "{}")
        except ValueError:
            continue
        answers = body.get("answers") or {}
        sound = (answers.get("sound") or {}).get("noul")
        nxt = (answers.get("next") or {}).get("choice")
        if sound is None and nxt is None:
            continue
        out.append({"model": body.get("served_by") or body.get("model"),
                    "sound": sound, "next": nxt,
                    "shadow": body.get("shadow") or [],
                    "cut": body.get("cut") or {}})
    return out


def _review_case(s: dict, origin: dict) -> dict:
    return {"seam": "review", "kind": "noul", "truth": None,
            "label_source": "pending", "origin": origin,
            "live": s.get("live") or [],
            "state": {"question": bound_text(s.get("question") or ""),
                      "answer": bound_text(s["final_answer"])},
            "instructions": REVIEW_Q}


def _accepted(db: Path) -> Optional[str]:
    """The sid whose answer the reviewer accepted: the newest session of
    a consultancy whose ``consultancy.json`` says ``accepted``."""
    try:
        body = json.loads((db.parent / "consultancy.json").read_text())
    except (OSError, ValueError):
        return None
    if body.get("status") != "accepted":
        return None
    children = [c for c in body.get("child_sids") or [] if c]
    return children[-1] if children else body.get("root_sid")


def review_candidates(sessions: dict, accepted: set) -> list[dict]:
    out = []
    for sid, s in sessions.items():
        parent = sessions.get(s.get("parent_sid") or "")
        if not parent or not parent.get("final_answer"):
            continue
        out.append(_review_case(parent, {
            "sid": parent["sid"], "followed_by": sid,
            "follow_up": (s.get("question") or "")[:3000]}))
    for sid in sorted(accepted):
        s = sessions.get(sid)
        if s and s.get("final_answer"):
            case = _review_case(s, {"sid": sid, "accepted": True})
            case.update(truth=True, label_source="accepted")
            out.append(case)
    return out


# --- main ------------------------------------------------------------------ #

def _final_answer(db: Path) -> str:
    md = db.parent / "metadata.json"
    try:
        return str(json.loads(md.read_text()).get("final_answer") or "")
    except (OSError, ValueError):
        return ""


def assemble(out: Path, *, progressing: int = 40, seed: int = 5) -> list[dict]:
    """``cases.json`` from ``candidates.json`` + ``labels.json``: every
    stuck and recovered struggle window, ``progressing`` progressing ones
    (one per lane), and every citation and review case with a reading in
    ``labels.json`` (``{"<seam>": {"<id>": [truth, reason, "visible"?]}}``)."""
    cand = {c["id"]: c for c in
            json.loads((out / "candidates.json").read_text())["cases"]}
    labels = json.loads((out / "labels.json").read_text())
    rng = random.Random(seed)
    st = [c for c in cand.values() if c["seam"] == "struggle"]
    cases = [c for c in st if c["candidate"] in ("stuck", "recovered")]
    prog = [c for c in st if c["candidate"] == "progressing"]
    rng.shuffle(prog)
    lanes: set = set()
    for c in prog:
        key = (c["origin"]["sid"], c["origin"]["round"], c["origin"]["lane"])
        if key in lanes or len(lanes) >= progressing:
            continue
        lanes.add(key)
        cases.append(c)
    cases += [c for c in cand.values() if c.get("label_source") == "accepted"]
    for seam in ("citations", "review"):
        for cid, v in (labels.get(seam) or {}).items():
            c = dict(cand[cid], truth=bool(v[0]), label_source="read",
                     label_reason=v[1])
            if "visible" in v[2:]:
                c["visible"] = True
            cases.append(c)
    (out / "cases.json").write_text(json.dumps({
        "version": 1,
        "about": ("H6: cases mined from real transcript.db councils, "
                  "labelled from the lane's own later calls (struggle) or "
                  "by a reading recorded in labels.json (citations, "
                  "review). Local only: councils from other repos carry "
                  "their code."),
        "cases": cases}, indent=1) + "\n")
    return cases


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--roots", default="/srv/dev-disk-by-label-opt/dev")
    p.add_argument("--per-lane", type=int, default=2,
                   help="at most this many struggle windows per lane label")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--assemble", action="store_true",
                   help="build cases.json from candidates.json + labels.json")
    args = p.parse_args(argv)
    rng = random.Random(args.seed)
    if args.assemble:
        out = args.out or ROOT / "benchmarks/consultants/results" / \
            dt.date.today().isoformat() / "decision-h6"
        from collections import Counter
        cases = assemble(out)
        print(len(cases), Counter((c["seam"], c["truth"]) for c in cases))
        return 0

    cases: list[dict] = []
    sessions: dict = {}
    accepted: set = set()
    dbs = transcripts(args.roots.split(","))
    for db in dbs:
        try:
            con = _ro(db)
        except sqlite3.Error:
            continue
        try:
            meta = _meta(con)
            if not meta.get("sid"):
                continue
            meta["project"] = os.path.basename(meta.get("cwd") or "")
            final = _final_answer(db)
            sessions[meta["sid"]] = {**meta, "final_answer": final,
                                     "live": live_scores(con)}
            acc = _accepted(db)
            if acc:
                accepted.add(acc)
            goal = _plan(con) or meta.get("question") or ""
            st = struggle_cases(con, meta, goal)
            by_label: dict = {}
            for c in st:
                key = (c["origin"]["round"], c["origin"]["lane"], c["candidate"])
                by_label.setdefault(key, []).append(c)
            for group in by_label.values():
                rng.shuffle(group)
                cases += group[:args.per_lane]
            reports = [_content(r) for (r,) in con.execute(
                "SELECT response_json FROM events WHERE kind='llm_call' "
                "AND role='researcher'")]
            cases += citation_cases([t for t in reports if t] + [final], meta)
        except sqlite3.Error:
            pass
        finally:
            con.close()
    cases += review_candidates(sessions, accepted)
    for c in cases:
        if "project" not in c:
            c["project"] = sessions.get(c["origin"]["sid"], {}).get("project", "")
    for i, c in enumerate(cases):
        c["id"] = f"{c['seam']}-{i:04d}"

    date = dt.date.today().isoformat()
    out = args.out or ROOT / "benchmarks/consultants/results" / date / "decision-h6"
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates.json").write_text(json.dumps(
        {"councils": len(dbs), "cases": cases}, indent=1) + "\n")
    from collections import Counter
    print(len(dbs), "councils")
    print(Counter((c["seam"], c.get("candidate") or c["truth"]) for c in cases))
    print(out / "candidates.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
