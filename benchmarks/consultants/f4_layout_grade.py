"""Blind grading for the PolyKV F4 layout A/B (ch-22).

Each council answer is scored against a reference key written from the
code (``questions.json``: ``id`` / ``question`` / ``key``) by two cloud
judges independently. A judge sees the question, the numbered key and one
answer under a blind id — never the arm. For each key point it says
``covered`` / ``partial`` / ``missing`` / ``contradicted``; the answer's
coverage is ``(covered + 0.5 × partial) / points``, its contradictions
are counted apart. Citations are also checked mechanically: a
``path:line`` resolves when one file under ``--root`` ends with that path
and has that line.

``--probe`` grades no-model adversary answers (empty, the question
restated, every name and number of the key with no relations, the key
with its numbers and conditions corrupted, and the key itself) and checks
the grader separates them, before any council is spent (bench rule:
the cheapest-strategy probe first).

Every call is saved (prompt, raw reply, parse) to ``calls.jsonl``.

    python -m benchmarks.consultants.f4_layout_grade --questions Q --probe --out DIR
    python -m benchmarks.consultants.f4_layout_grade --questions Q --answers DIR --root W --out DIR
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from benchmarks.consultants.harness import bench_client, timed_chat

JUDGES = ("glm-5.3-flash:cloud", "deepseek-v4.1-flash:cloud")
VERDICTS = ("covered", "partial", "missing", "contradicted")
MAX_CONCURRENT = 2          # Pro = 3 connections, the hooks hold one

SYSTEM = (
    "You grade an answer about a codebase against a reference key written "
    "from the code. The key is correct. For EACH numbered key point decide:\n"
    "- covered: the answer states the point's substance (its rule, condition "
    "and any number), in any wording;\n"
    "- partial: the answer gets part of it (e.g. the rule without its "
    "threshold, or the threshold without the condition it applies to);\n"
    "- missing: the answer does not address it;\n"
    "- contradicted: the answer states something the point says is false "
    "(a wrong number, an inverted condition, a wrong order).\n"
    "Naming a function, constant or file is NOT coverage by itself: a point "
    "is covered only when the answer says what the code does. A list of "
    "names without the relations between them covers nothing. Do not give "
    "credit for confident tone or length.\n\n"
    "Reply with JSON only, no prose and no code fence:\n"
    '{"points": [{"n": 1, "verdict": "covered|partial|missing|contradicted", '
    '"why": "<at most 15 words>"}, ...]}\n'
    "with one entry per key point, n from 1.")


def build_messages(question: str, key: list[str], answer: str) -> list[dict]:
    keytext = "\n".join(f"{i}. {k}" for i, k in enumerate(key, 1))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             f"QUESTION\n{question}\n\nREFERENCE KEY ({len(key)} points)\n"
             f"{keytext}\n\nANSWER TO GRADE\n<<<\n{answer}\n>>>"}]


def parse_verdicts(text: str, n: int) -> Optional[list[str]]:
    """The judge's verdict per point, or ``None`` when unreadable. A point
    the judge left out counts as missing."""
    # The last object carrying "points": a judge that reasons in prose
    # first (glm-5.3-flash) puts braces in the prose before it.
    data = None
    dec = json.JSONDecoder()
    for m in reversed(list(re.finditer(r'\{\s*"points"', text or ""))):
        try:
            data = dec.raw_decode(text, m.start())[0]
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return None
    out = ["missing"] * n
    seen = False
    for p in data.get("points") or []:
        try:
            i = int(p.get("n")) - 1
        except (TypeError, ValueError):
            continue
        v = str(p.get("verdict") or "").strip().lower()
        if 0 <= i < n and v in VERDICTS:
            out[i], seen = v, True
    return out if seen else None


def coverage(verdicts: list[str]) -> float:
    if not verdicts:
        return 0.0
    got = sum(1.0 if v == "covered" else 0.5 if v == "partial" else 0.0
              for v in verdicts)
    return got / len(verdicts)


# ------------------------------------------------------------ citations

_CITE = re.compile(r"`?((?:[\w.-]+/)*[\w.-]+\.(?:py|md|toml|json|sh|ts|js|yaml|yml)):"
                   r"(\d+)(?:[-–](\d+))?`?")


def citations(answer: str) -> list[tuple[str, int]]:
    return [(m.group(1), int(m.group(2))) for m in _CITE.finditer(answer or "")]


class Resolver:
    """``path:line`` → resolved under ``root`` by path suffix."""

    def __init__(self, root: Path):
        self.root = root
        self.files = [p for p in root.rglob("*")
                      if p.is_file() and ".git" not in p.parts
                      and ".claude-hooks" not in p.parts
                      and "node_modules" not in p.parts]
        self._lines: dict[Path, int] = {}

    def resolve(self, path: str, line: int, hint: str = "") -> bool:
        """``hint`` (the question) breaks a tie between same-named files:
        a bare ``model.py`` is the one whose path the question names."""
        rel = path.lstrip("./")
        # A bare name counts only when one file has it (the CitationLinter
        # never suffix-matches one; answers cite them anyway).
        hits = [p for p in self.files
                if str(p.relative_to(self.root)) == rel
                or str(p).endswith("/" + rel)]
        if len(hits) > 1 and hint:
            hits = [p for p in hits if str(p.relative_to(self.root)) in hint]
        if len(hits) != 1:
            return False
        p = hits[0]
        if p not in self._lines:
            try:
                self._lines[p] = len(p.read_text(errors="replace").splitlines())
            except OSError:
                self._lines[p] = 0
        return 1 <= line <= self._lines[p]


def cite_stats(answer: str, resolver: Optional[Resolver], hint: str = "") -> dict:
    cs = citations(answer)
    if resolver is None:
        return {"cited": len(cs)}
    ok = sum(resolver.resolve(p, n, hint) for p, n in cs)
    return {"cited": len(cs), "resolved": ok,
            "resolved_share": round(ok / len(cs), 3) if cs else None}


# ------------------------------------------------------------ the judges

class Grader:
    def __init__(self, base: str, out: Path, judges=JUDGES):
        self.judges = judges
        self.clients = {j: bench_client(j, base) for j in judges}
        self.out = out
        self.usage: dict = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(MAX_CONCURRENT)

    def _log(self, rec: dict) -> None:
        with self._lock, open(self.out / "calls.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def judge(self, judge: str, qid: str, blind: str, question: str,
              key: list[str], answer: str) -> Optional[list[str]]:
        msgs = build_messages(question, key, answer)
        for attempt in range(2):
            with self._sem:
                t0 = time.monotonic()
                try:
                    resp = timed_chat(self.clients[judge], {
                        "model": judge, "messages": msgs, "stream": False,
                        "think": False, "options": {"temperature": 0.0}},
                        self.usage, f"judge:{judge}", judge)
                    text = ((resp.get("choices") or [{}])[0].get("message")
                            or {}).get("content") or ""
                    err = None
                except Exception as e:  # noqa: BLE001 — recorded, retried once
                    text, err, resp = "", str(e)[:300], {}
            verdicts = parse_verdicts(text, len(key))
            self._log({"judge": judge, "qid": qid, "blind": blind,
                       "attempt": attempt, "wall_s": round(time.monotonic() - t0, 2),
                       "error": err, "messages": msgs, "reply": text,
                       "usage": resp.get("usage") if isinstance(resp, dict) else None,
                       "verdicts": verdicts})
            if verdicts is not None:
                return verdicts
        return None

    def grade(self, items: list[dict], questions: dict,
              resolver: Optional[Resolver] = None) -> list[dict]:
        """``items``: ``{"blind", "qid", "answer", ...}``; each gets
        ``judges`` (verdicts per judge), ``coverage`` (mean over judges
        that answered), ``contradicted`` and the citation stats."""
        jobs = {}
        with cf.ThreadPoolExecutor(MAX_CONCURRENT * 2) as ex:
            for it in items:
                q = questions[it["qid"]]
                for j in self.judges:
                    jobs[ex.submit(self.judge, j, it["qid"], it["blind"],
                                   q["question"], q["key"], it["answer"])] = (it, j)
            for fut in cf.as_completed(jobs):
                it, j = jobs[fut]
                it.setdefault("judges", {})[j] = fut.result()
        for it in items:
            got = [v for v in it["judges"].values() if v is not None]
            it["coverage"] = (round(sum(coverage(v) for v in got) / len(got), 3)
                              if got else None)
            it["contradicted"] = (round(sum(v.count("contradicted") for v in got)
                                        / len(got), 2) if got else None)
            it["agreement"] = (round(sum(a == b for a, b in zip(*got)) / len(got[0]), 3)
                               if len(got) == 2 else None)
            it.update(cite_stats(it["answer"], resolver,
                                 questions[it["qid"]]["question"]))
        return items


# ------------------------------------------------------------ the probe

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\(\)|[A-Z][A-Z0-9_]{3,}|\b[a-z]+_[a-z_]+\b"
                   r"|\b\d[\d,.]*\b|[\w/]+\.py")


def probe_answers(q: dict) -> dict[str, str]:
    key = q["key"]
    names = []
    for k in key:
        for m in _NAME.findall(k):
            if m not in names:
                names.append(m)

    def corrupt(text: str) -> str:
        text = re.sub(r"\b(\d+)\b", lambda m: str(int(m.group(1)) * 2 + 3), text)
        for a, b in (("never", "always"), ("only", "also"), ("unless", "when"),
                     ("not ", ""), ("before", "after"), ("first", "last")):
            text = text.replace(a, f"\0{b}")
        return text.replace("\0", "")

    return {
        "empty": "I could not determine this from the code.",
        "restate": q["question"],
        "names": ("The relevant pieces are: " + ", ".join(names)
                  + ". See the module for the details."),
        "corrupt": "\n".join(f"- {corrupt(k)}" for k in key),
        "oracle": "\n".join(f"- {k}" for k in key),
    }


#: What the grader must give each probe for the probe to pass.
PROBE_BOUNDS = {"empty": ("max", 0.1), "restate": ("max", 0.15),
                "names": ("max", 0.3), "corrupt": ("max", 0.45),
                "oracle": ("min", 0.85)}


def run_probe(grader: Grader, questions: dict) -> dict:
    items = []
    for qid, q in questions.items():
        for kind, ans in probe_answers(q).items():
            items.append({"blind": f"probe-{qid}-{kind}", "qid": qid,
                          "kind": kind, "answer": ans})
    grader.grade(items, questions)
    by_kind: dict = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)
    verdict = {}
    for kind, its in by_kind.items():
        covs = [i["coverage"] for i in its if i["coverage"] is not None]
        mean = sum(covs) / len(covs) if covs else None
        side, bound = PROBE_BOUNDS[kind]
        ok = mean is not None and (mean <= bound if side == "max" else mean >= bound)
        verdict[kind] = {"mean_coverage": None if mean is None else round(mean, 3),
                         "worst": (max(covs) if side == "max" else min(covs)) if covs else None,
                         "bound": f"{side} {bound}", "pass": ok,
                         "mean_contradicted": round(sum(i["contradicted"] or 0 for i in its)
                                                    / len(its), 2)}
    return {"items": items, "verdict": verdict,
            "pass": all(v["pass"] for v in verdict.values())}


def blind_id(sid: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{sid}".encode()).hexdigest()[:8]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--answers", help="dir of <blind>.json {qid, answer}")
    ap.add_argument("--root", help="tree the councils read, for citations")
    ap.add_argument("--base", default="")
    a = ap.parse_args(argv)
    from claude_hooks import ollama_upstream
    base = a.base or ollama_upstream.url()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    questions = {q["id"]: q for q in json.loads(Path(a.questions).read_text())}
    grader = Grader(base, out)
    if a.probe:
        res = run_probe(grader, questions)
        (out / "probe.json").write_text(json.dumps(res, indent=1))
        print(json.dumps(res["verdict"], indent=1))
        print("usage", json.dumps(grader.usage))
        return 0 if res["pass"] else 1
    items = [json.loads(p.read_text()) for p in sorted(Path(a.answers).glob("*.json"))]
    resolver = Resolver(Path(a.root)) if a.root else None
    grader.grade(items, questions, resolver)
    (out / "grades.json").write_text(json.dumps(items, indent=1))
    for it in items:
        print(it["blind"], it["qid"], it["coverage"], it["contradicted"],
              it.get("resolved_share"))
    print("usage", json.dumps(grader.usage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
