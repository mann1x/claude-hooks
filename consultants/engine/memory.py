"""The council's memory (docs/PLAN-council-memory.md, M2).

Councils started cold: runs 3–5 found real defects and the next run did
not hear of them, and the critic re-ranked deliberate design every run.
The findings ledger (:mod:`claude_hooks.findings`) is the project's
record of what councils claimed and what the user or the assistant
decided about it. Here the council uses it:

- :func:`with_known_findings` gives the planner, critic, meta-critic and
  synthesizer the findings relevant to the question, each status with
  what the role should do with it. An empty ledger changes nothing.
- :func:`record_answer` records what a finished answer ranked or marked
  in its matrix, citing a run that reached the code, as candidates (or
  as evidence on the finding it restates). It never settles: only the
  user or the assistant does (``Ledger.settle``).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

KNOWN_MAX = 12
CLAIM_CHARS = 400

#: What each role is told to do with a finding of each status.
_DO = {
    "planner": {
        "confirmed": "re-verify it with its reproduction; do not spend a "
                     "lane rediscovering it",
        "fixed": "check that the fix holds (run its reproduction)",
        "refuted": "do not plan it again unless the question brings new "
                   "evidence against the decision",
        "by_design": "intended; plan a check only where the question or "
                     "new behaviour contradicts the stated reason",
        "candidate": "unsettled: plan the run that would settle it",
    },
    "critic": {
        "confirmed": "known real; a report that restates it is not new",
        "fixed": "should hold; a report that shows it broken again is new "
                 "evidence — name the run",
        "refuted": "a report that re-raises it needs evidence against the "
                   "recorded decision, not the old claim",
        "by_design": "intended, for the reason given. You may still "
                     "challenge it, but only with behaviour that "
                     "contradicts that reason, shown by a run",
        "candidate": "unsettled: what would settle it",
    },
    "synthesizer": {
        "confirmed": "tag a ranked item that restates it `(known: <id>)`",
        "fixed": "if a run shows it broken again, rank it as a regression "
                 "`(regressed: <id>)`",
        "refuted": "do not rank it again without a run against the "
                   "recorded decision",
        "by_design": "do not rank it as a defect unless a run contradicts "
                     "the recorded reason; say so if one does",
        "candidate": "tag a ranked item that restates it `(known: <id>)`",
    },
}
_DO["meta_critic"] = _DO["critic"]

HEADER = (
    "KNOWN FINDINGS (this project's ledger: what earlier councils found and "
    "what the user or the assistant decided). Settled findings are "
    "decisions, not suggestions; a claim against one needs a run.")
TAIL = {
    "synthesizer": ("Tag every ranked item `(known: <id>)` when it restates "
                    "one of these, else `(new)`."),
}

_PATH = re.compile(r"[\w./-]+\.\w{1,6}(?::\d+)?")


def paths_in(text: str) -> list[str]:
    return [p for p in _PATH.findall(text or "") if "/" in p or ":" in p][:40]


def known_findings(cwd: str, question: str, *, k: int = KNOWN_MAX) -> list:
    """The ledger's findings relevant to ``question``; ``[]`` when the
    project has no ledger or it cannot be read."""
    if not cwd:
        return []
    try:
        from claude_hooks.findings import Ledger, findings_enabled
        from claude_hooks.findings.ledger import findings_root
        if not findings_enabled():
            return []
        lg = Ledger(findings_root(cwd))
        if not lg.exists:
            return []
        return lg.relevant(question, paths_in(question), k=k)
    except Exception:  # noqa: BLE001 — memory never fails a council
        log.exception("known findings unreadable for %s", cwd)
        return []


def _one(f, do: dict) -> str:
    claim = " ".join((f.claim or f.title).split())
    claim = claim if len(claim) <= CLAIM_CHARS else claim[:CLAIM_CHARS] + "…"
    bits = [f"- {f.id} [{f.status}] {f.title}"]
    if f.paths:
        bits.append(f"  at {', '.join(f.paths[:3])}")
    bits.append(f"  claim: {claim}")
    if f.decision:
        bits.append(f"  decision ({f.settled_by}): "
                    + " ".join(f.decision.split())[:CLAIM_CHARS])
    if f.commit:
        bits.append(f"  fixed in {f.commit}")
    if f.reproduce:
        bits.append(f"  reproduce: {' '.join(f.reproduce.split())[:300]}")
    bits.append(f"  → {do.get(f.status, '')}")
    return "\n".join(bits)


def findings_block(findings: list, role: str) -> str:
    if not findings:
        return ""
    do = _DO.get(role, _DO["critic"])
    out = [HEADER] + [_one(f, do) for f in findings]
    if role in TAIL:
        out.append(TAIL[role])
    return "\n".join(out)


def with_known_findings(msgs: list[dict], state: dict, role: str) -> list[dict]:
    """``msgs`` with the known findings appended to the last user turn;
    unchanged when there are none."""
    if not msgs or msgs[-1].get("role") != "user":
        return msgs
    block = findings_block(
        known_findings(state.get("cwd") or "", state.get("question") or ""),
        role)
    if not block:
        return msgs
    return msgs[:-1] + [{**msgs[-1], "content": (
        str(msgs[-1].get("content") or "") + "\n\n" + block)}]


def _evidence(it: dict) -> str:
    parts = []
    for r in it["runs"]:
        first = next((ln.strip() for ln in (r.get("printed") or r.get("tail")
                                            or "").split("\n") if ln.strip()), "")
        parts.append(f"{r['id']} [{r.get('role')} lane {r.get('lane')}] "
                     f"`{' '.join((r.get('command') or '').split())[:160]}` → "
                     f"{r.get('exit')}" + (f": {first[:200]}" if first else ""))
    return "; ".join(parts)


def record_answer(cwd: str, sid: str, answer: str,
                  rows: Optional[list[dict]]) -> list[tuple[str, bool]]:
    """Record a finished answer's ranked items and matrix rows that cite a
    run that reached the code. ``[(finding id, created)]``; ``[]`` with
    no shell (``rows`` None), no such items, or the ledger off."""
    if not cwd or not answer or rows is None or not os.path.isdir(cwd):
        return []
    try:
        from claude_hooks.findings import Ledger, findings_enabled
        from claude_hooks.findings.ledger import findings_root
        from consultants.engine import evidence
        if not findings_enabled():
            return []
        items = evidence.claim_audit_items(answer, rows)
        if not items:
            return []
        lg = Ledger(findings_root(cwd))
        out = []
        for it in items:
            text = " ".join(it["text"].split())
            title = evidence._label(it["text"])[:160]
            f, created = lg.record(
                title, text, paths=paths_in(it["text"]),
                severity="H" if it.get("kind") != "cell" else "M",
                evidence=_evidence(it), council=sid,
                reproduce=" ".join((it["runs"][0].get("command") or "").split()))
            out.append((f.id, created))
        return out
    except Exception:  # noqa: BLE001 — memory never fails a council
        log.exception("recording council %s findings failed", sid)
        return []


__all__ = ["with_known_findings", "record_answer", "findings_block",
           "known_findings", "paths_in"]
