"""Two peer judges and a synthesizer: the coder_bench quality verdict.

Both judges score the same code on the same blind rubric
(``harness.build_judge_system``), independently. When both return a
score, a synthesizer reads the code and the two reviews and gives the
final score; when they disagree it must check each review's specific
claim against the code and discard the ones the code refutes. When only
one judge returns a score, that score stands.

Why two cheap judges rather than one strong one (2026-09-23,
``results/2026-09-23/judge_eval``): glm-5.3-flash separates passing from
failing code best (AUC 0.925) and repeats itself (89 % exact on
retest), and deepseek-v4.1-flash has no measurable self-bias and answers
in 1.8 s. Each covers the other's weakness, and both together cost less
than one kimi-k2.6 verdict.

The synthesizer sees the reviews as **A** and **B**, never the model
names, in an order fixed by a hash of the trial. The default synth is
deepseek-v4.1-flash, which is also one of the reviewers: without the
names it cannot prefer its own review for being its own, and the
hashed order keeps a position preference from always favouring the
same judge.
"""
from __future__ import annotations

import hashlib
import re
from typing import Callable, Optional

from benchmarks.consultants.harness import parse_judge_response

DEFAULT_PANEL = ("glm-5.3-flash:cloud", "deepseek-v4.1-flash:cloud")
DEFAULT_SYNTH = "deepseek-v4.1-flash:cloud"


def build_synth_system(language: str = "Python") -> str:
    return (
        f"You are a senior {language} code reviewer settling a review. "
        "Two reviewers independently scored the same code with this "
        "rubric:\n\n"
        "1 — Broken. Doesn't solve the task or has obvious bugs.\n"
        "2 — Solves the basic case but misses obvious edge cases or "
        "uses confusing structure.\n"
        f"3 — Correct for the spec, but overly verbose / non-idiomatic / "
        f"missing simple {language} idioms.\n"
        "4 — Correct and idiomatic. Reasonable structure, edge cases "
        "considered.\n"
        "5 — Excellent. Minimal, idiomatic, robust. The kind of code "
        "you'd ship without changes.\n\n"
        "Read the code yourself. For each review, decide whether the "
        "specific claim it makes (a bug, a missed edge case, an idiom "
        "problem, or that the code is clean) is actually true of THIS "
        "code. When the scores differ, trace the code on the input the "
        "claim concerns before deciding. A claim the code refutes "
        "counts for nothing; do not split the difference. Judge "
        "correctness and robustness first; do not reward or punish a "
        "layout, naming or comment style.\n\n"
        "Output EXACTLY three lines:\n"
        "Line 1: ``SCORE: <integer 1-5>``\n"
        "Line 2: ``CLAIMS: A=<HOLDS|REFUTED>, B=<HOLDS|REFUTED>``\n"
        "Line 3: One short sentence (max 30 words) justifying the score.\n\n"
        "Do not add preamble, headings, or markdown."
    )


def build_synth_messages(task: str, code: str, reviews: list[tuple],
                         *, language: str = "Python",
                         fence: str = "python") -> list[dict]:
    """``reviews`` are ``(score, rationale)`` in the order to show them,
    labelled A, B, ..."""
    blocks = "\n".join(
        f"REVIEW {chr(65 + i)}: SCORE {int(s)} — {r.strip() or '(no reason given)'}"
        for i, (s, r) in enumerate(reviews))
    user = (
        f"TASK:\n{task.strip()}\n\n"
        f"CODE:\n```{fence}\n{code}\n```\n\n"
        f"{blocks}\n\n"
        "Settle the score. Three lines only."
    )
    return [{"role": "system", "content": build_synth_system(language)},
            {"role": "user", "content": user}]


_CLAIM_RE = re.compile(r"\b([A-Z])\s*=\s*(HOLDS|REFUTED)\b", re.IGNORECASE)


def parse_synth_response(text: str) -> tuple[Optional[float], dict, str]:
    """``(score, {"A": "HOLDS", ...}, rationale)``."""
    score, _ = parse_judge_response(text or "")
    claims: dict = {}
    reason = ""
    for line in (text or "").strip().splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^\s*SCORE\s*[:=]", s, re.IGNORECASE):
            continue
        if re.match(r"^\s*CLAIMS\s*[:=]", s, re.IGNORECASE):
            claims = {k.upper(): v.upper() for k, v in _CLAIM_RE.findall(s)}
            continue
        reason = s
    return score, claims, reason[:300]


def swapped(key: str) -> bool:
    """Whether this trial shows the reviews in reverse panel order."""
    return hashlib.sha256(key.encode()).digest()[0] & 1 == 1


def resolve(verdicts: list[tuple], *, key: str,
            synth: Optional[Callable[[list[dict]], str]],
            task: str, code: Optional[str],
            language: str = "Python", fence: str = "python") -> dict:
    """The panel's final verdict.

    ``verdicts`` are ``(model, score, rationale)`` in panel order.
    ``synth(messages) -> text`` makes the synthesizer call (it may
    raise). Returns ``score``, ``rationale``, ``source`` (``synth`` /
    ``single`` / ``agreed`` / ``mean`` / ``none``), ``discordant``,
    ``claims`` keyed by the panel model, and ``synth_error``.
    """
    scored = [v for v in verdicts if v[1] is not None]
    out = {"score": None, "rationale": "", "source": "none",
           "discordant": None, "claims": {}, "synth_error": None}
    if not scored:
        return out
    if len(scored) == 1:
        out.update(score=scored[0][1], rationale=scored[0][2], source="single")
        return out
    out["discordant"] = len({v[1] for v in scored}) > 1
    shown = list(reversed(scored)) if swapped(key) else list(scored)
    err = None
    if synth is not None and code is not None:
        msgs = build_synth_messages(task, code, [(v[1], v[2]) for v in shown],
                                    language=language, fence=fence)
        try:
            text = synth(msgs)
            if not (text or "").strip():
                text = synth(msgs)  # one retry on silence, as the judges do
            score, claims, reason = parse_synth_response(text)
            if score is not None:
                out.update(score=score, rationale=reason, source="synth",
                           claims={shown[ord(k) - 65][0]: v
                                   for k, v in claims.items()
                                   if 0 <= ord(k) - 65 < len(shown)})
                return out
            err = f"synth unparseable: {(text or '').strip()[:120]!r}"
        except Exception as e:  # noqa: BLE001 — fall back, recorded
            err = f"{type(e).__name__}: {e}"
    out["synth_error"] = err or "no synthesizer"
    if not out["discordant"]:
        out.update(score=scored[0][1], rationale=scored[0][2], source="agreed")
    else:
        out.update(score=sum(v[1] for v in scored) / len(scored),
                   rationale="; ".join(v[2] for v in scored)[:300],
                   source="mean")
    return out


def label(panel: list[str], synth: str) -> str:
    """The name a panel's verdicts are filed under: the synthesizer,
    then the reviewers — ``deepseek-v4.1-flash:cloud#panel=a+b``."""
    return f"{synth}#panel=" + "+".join(panel)
