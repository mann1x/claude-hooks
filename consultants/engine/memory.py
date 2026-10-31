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
#: Findings added for the files the research reports cite, on top of the
#: question's (ch-13, after SCM's recall on what the turn touches): keyed on
#: the question alone, a settled finding about a file the reports cite never
#: reached the judges unless the question named that file. Offline replay
#: over the 8 cline-sync councils of 2026-10-18 (current ledger): 5-9 added
#: each, csf-13/14 (refuted) in 7-8, csf-11 (by_design) in 4, nothing taken
#: away; at most 9, so the bound holds all of them (a cap of 6 kept the
#: fixed ones and dropped the settled).
REPORT_EXTRA = 12
REPORT_PATHS_MAX = 200
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
        "by_design": "intended, for the reason given. A report that lists "
                     "it as a defect, a risk or an improvement restates "
                     "the decision; it may still challenge it, but only "
                     "with behaviour that contradicts that reason, shown "
                     "by a run",
        "candidate": "unsettled: what would settle it",
    },
    "synthesizer": {
        "confirmed": "tag a ranked item that restates it `(known: <id>)`",
        "fixed": "if a run shows it broken again, rank it as a regression "
                 "`(regressed: <id>)`",
        "refuted": "do not rank it again, or list it as a risk or an "
                   "improvement, without a run against the recorded "
                   "decision",
        "by_design": "do not rank it as a defect, list it as a risk, or "
                     "propose undoing it as an improvement, unless a run "
                     "contradicts the recorded reason; say so if one does",
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


def report_paths(reports) -> list[str]:
    """The files the research reports cite, in order, each once."""
    found = [p for r in reports or () for p in _PATH.findall(str(r or ""))
             if "/" in p or ":" in p]
    return list(dict.fromkeys(found))[:REPORT_PATHS_MAX]


def known_findings(cwd: str, question: str, *, k: int = KNOWN_MAX,
                   reports=()) -> list:
    """The ledger's findings relevant to ``question``, then up to
    :data:`REPORT_EXTRA` more at a file the ``reports`` cite (a shared
    place, not shared words; never displacing the question's); ``[]`` when
    the project has no ledger or it cannot be read."""
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
        asked = paths_in(question)
        found = lg.relevant(question, asked, k=k)
        cited = report_paths(reports)
        if cited:
            from claude_hooks.findings.ledger import place_score
            have = {f.id for f in found}
            found += [f for f in lg.relevant(question, asked + cited,
                                             k=k + REPORT_EXTRA)
                      if f.id not in have and place_score(f.paths, cited) > 0
                      ][:REPORT_EXTRA]
        return found
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
        known_findings(state.get("cwd") or "", state.get("question") or "",
                       reports=state.get("research") or ()),
        role)
    if not block:
        return msgs
    return msgs[:-1] + [{**msgs[-1], "content": (
        str(msgs[-1].get("content") or "") + "\n\n" + block)}]


# -------------------------------------- settled findings restated (run 8)
#: Statuses an answer must not list again without a run against them.
SETTLED_AGAINST = ("by_design", "refuted")
SETTLED_ITEMS_MAX = 80
_SETTLED_SKIP = re.compile(r"unverified|dropped|held patch|sandbox", re.I)
_ITEM_LINE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+\S|^\s*\|")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")
SETTLED_AUDIT_SYSTEM = (
    "You compare a council's answer with decisions already made about this "
    "project. SETTLED lists findings decided to be intended (by_design) or "
    "not real (refuted), each with the reason recorded. ITEMS are entries "
    "of the answer: defects, risks, improvements, matrix rows. For each "
    "item that restates a settled finding — names the same behaviour as a "
    "defect, a risk, or proposes changing it as an improvement — answer "
    "one line `I<k>: S<j> — <what it restates>`. An item that only "
    "mentions the same file, or names a different behaviour, is not a "
    "restatement: say nothing about it. Answer nothing else; no line at "
    "all when no item restates one.")
SETTLED_NOTE = ("harness: this restates {id}, settled {status} — {decision} "
                "Listing it needs a run against that reason")
#: Council run 9: lane 0 was challenged with csf-12, read it, conceded
#: the line-span claim and narrowed it to what csf-12 never decided; the
#: answer still said the item "needs a run against that reason". The
#: cross-examination was that run.
EXAMINED_NOTE = ("harness: close to {id}, settled {status}; cross-examined "
                 "against it in round {round} and ruled {ruling} — {why}")
_SETTLED_LINE = re.compile(r"^\W*I(\d{1,3})\W*[:.)\]-]\s*\**S(\d{1,3})\b"
                           r"\**\s*(?:[—–:-]+\s*)?(.*)$", re.M)


def settled_findings(cwd: str, question: str, reports=()) -> list:
    """The by_design and refuted findings relevant to ``question`` and the
    files the ``reports`` cite: the same set the judges were shown."""
    return [f for f in known_findings(cwd, question, reports=reports)
            if f.status in SETTLED_AGAINST]


def answer_items(text: str) -> list[dict]:
    """``{start, text}`` of each list item and table row outside the
    Unverified / Dropped / Held patches / Sandbox sections."""
    from consultants.engine import evidence
    lines = (text or "").split("\n")
    out = []
    for heading, a, b in evidence._sections(lines):
        if _SETTLED_SKIP.search(heading):
            continue
        for i in range(a, b):
            ln = lines[i]
            if not _ITEM_LINE.match(ln) or _TABLE_RULE.match(ln):
                continue
            if ln.strip().startswith("|") and i + 1 < b \
                    and _TABLE_RULE.match(lines[i + 1]):
                continue                      # a table's header row
            out.append({"start": i, "text": ln.strip()})
    return out[:SETTLED_ITEMS_MAX]


def settled_audit_prompt(findings: list, items: list[dict]) -> str:
    sett = "\n".join(
        f"S{j}. {f.id} [{f.status}] {f.title}\n    reason: "
        + " ".join((f.decision or "").split())
        for j, f in enumerate(findings, start=1))
    its = "\n".join(f"I{k}. {it['text']}" for k, it in enumerate(items, start=1))
    return f"SETTLED:\n{sett}\n\nITEMS:\n{its}"


def parse_settled_audit(reply: str, n_items: int, n_findings: int
                        ) -> dict[int, int]:
    out: dict[int, int] = {}
    for m in _SETTLED_LINE.finditer(reply or ""):
        k, j = int(m.group(1)), int(m.group(2))
        if 1 <= k <= n_items and 1 <= j <= n_findings and k not in out:
            out[k] = j
    return out


def examined_against(findings: list, rulings) -> dict:
    """``{finding id: ruling record}`` for each settled finding a
    cross-examination challenge or answer names, where the critic ruled
    the claim stands or narrowed (``cross_exam.records``); the latest
    round wins."""
    out: dict = {}
    for r in sorted(rulings or [], key=lambda r: int(r.get("round") or 0)):
        if not isinstance(r, dict) or r.get("ruling") not in ("stands", "narrowed"):
            continue
        said = " ".join([*map(str, r.get("challenges") or []), str(r.get("answer") or "")])
        for f in findings:
            if re.search(rf"(?<![\w-]){re.escape(f.id)}(?![\w-])", said):
                out[f.id] = r
    return out


def apply_settled_audit(text: str, findings: list, items: list[dict],
                        hits: dict[int, int], examined: Optional[dict] = None
                        ) -> tuple[str, int]:
    """The answer with a note on every item that restates a settled
    finding (it is never removed: a run against the reason may be in
    it); ``(text, notes)``. A finding in ``examined``
    (:func:`examined_against`) gets the cross-examination's ruling
    instead of the demand for a run."""
    examined = examined or {}
    if not hits:
        return text, 0
    lines = text.split("\n")
    n = 0
    for k, j in hits.items():
        it, f = items[k - 1], findings[j - 1]
        i = it["start"]
        if f.id in lines[i] and ("harness: this restates" in lines[i]
                                 or "harness: close to" in lines[i]):
            continue
        ex = examined.get(f.id)
        if ex:
            why = " ".join(str(ex.get("why") or "").split()) or "no reason given"
            why = (why if len(why) <= 220 else why[:219] + "…").rstrip(".") + "."
            note = "_(" + EXAMINED_NOTE.format(
                id=f.id, status=f.status, round=ex.get("round") or "?",
                ruling=ex.get("ruling"), why=why) + ")_"
        else:
            dec = " ".join((f.decision or "").split())
            dec = (dec if len(dec) <= 220 else dec[:219] + "…").rstrip(".") + "."
            note = "_(" + SETTLED_NOTE.format(id=f.id, status=f.status,
                                              decision=dec) + ")_"
        row = lines[i].rstrip()
        if row.lstrip().startswith("|"):
            cut = row[:-1].rstrip() if row.endswith("|") else row
            lines[i] = f"{cut} {note} |"
        else:
            lines[i] = f"{row} {note}"
        n += 1
    return "\n".join(lines), n


def _evidence(it: dict) -> str:
    parts = []
    for r in it["runs"]:
        first = next((ln.strip() for ln in (r.get("printed") or r.get("tail")
                                            or "").split("\n") if ln.strip()), "")
        parts.append(f"{r['id']} [{r.get('role')} lane {r.get('lane')}] "
                     f"`{' '.join((r.get('command') or '').split())[:160]}` → "
                     f"{r.get('exit')}" + (f": {first[:200]}" if first else ""))
    return "; ".join(parts)


#: The column of a ranked table that says what the defect is.
_DEFECT_COLUMN = re.compile(r"defect|finding|issue|failure|problem|"
                            r"description|claim|what goes wrong", re.I)
_TAG_CELL = re.compile(r"^\W*(?:#?\d+|new|known|[a-z]{1,6}f-\d+)\W*$", re.I)


def _title(it: dict, lines: list[str]) -> str:
    """A finding's title from what the answer says the defect is (run 7:
    titles came out "new — `grep.ts:260`" from the ID and path columns)."""
    from consultants.engine import evidence
    row = lines[it["start"]] if it["start"] < len(lines) else it["text"]
    if not row.strip().startswith("|"):
        return evidence._label(it["text"])[:160]
    cells = [c.strip() for c in evidence._cells(row)]
    head = evidence._header(lines, it["start"])
    pick = next((cells[k] for k, h in enumerate(head)
                 if k < len(cells) and _DEFECT_COLUMN.search(h) and cells[k]), "")
    if not pick:
        pick = next((c for c in cells if not _TAG_CELL.match(c)
                     and not re.fullmatch(r"[`\s]*[\w./-]+\.\w{1,6}(?::\d+)?[`\s/]*.*",
                                          c) and len(c.split()) >= 3), "")
    pick = re.sub(r"\*\*|__|`", "", pick or evidence._label(it["text"]))
    pick = " ".join(pick.split()).rstrip(".;:,")
    return pick if len(pick) <= 160 else pick[:159] + "…"


#: Marks of a matrix cell that say the case is mishandled.
_DEFECT_MARK = re.compile(r"[⚠✗✘❌]")


def _cell_title(it: dict) -> str:
    """``<row> — <column>: <what the mark means>`` (run 8: a row of six
    cells, five of them ``not tested``, was titled by one of those)."""
    from consultants.engine import evidence
    row = re.sub(r"\*\*|__|`", "", evidence._cells(it["text"])[0]).strip()
    col = re.sub(r"\*\*|__|`", "", it.get("header") or "").strip()
    mean = evidence._meaning(it["cell"], it.get("legend") or {})
    t = " ".join(f"{row} — {col}: {mean}".split())
    return t if len(t) <= 160 else t[:159] + "…"


def _tag(text: str, prefix: str) -> str:
    """The ledger id an item is tagged with (``(known: csf-7)``, an ID
    cell ``**csf-7**``); "" when none."""
    m = re.search(rf"(?<![\w-])({re.escape(prefix)}-\d+)(?![\w-])", text or "")
    return m.group(1) if m else ""


#: A ranked item the answer calls new.
_NEW_LABEL = re.compile(r"\(\s*new\s*\)", re.I)
KNOWN_NOTE = ("harness: the ledger already has this as {id} ({status}): "
              "{title}")


def ruled_out_ids(cwd: str) -> set[str]:
    """Ids the ledger settled as no defect (``refuted`` / ``by_design``).
    A brief names them to forbid them, so nothing owes a run on them
    (run 10b listed csf-11..14 as "not re-verified"). Empty with no
    ledger; never raises."""
    if not cwd:
        return set()
    try:
        from claude_hooks.findings import (
            BY_DESIGN, REFUTED, Ledger, findings_enabled)
        from claude_hooks.findings.ledger import findings_root
        if not findings_enabled():
            return set()
        lg = Ledger(findings_root(cwd))
        if not lg.exists:
            return set()
        return {f.id.lower() for f in lg.iter_findings()
                if f.status in (REFUTED, BY_DESIGN)}
    except Exception:  # noqa: BLE001 — memory never fails a council
        log.exception("ruled-out lookup failed")
        return set()


def note_known_new(cwd: str, answer: str) -> tuple[str, int]:
    """The answer with a note on each ranked item labelled "(new)" that
    the ledger already holds (``Ledger.match``, no write): council run 9
    called csf-20 new. ``(text, notes)``; the answer unchanged with no
    ledger. Never raises."""
    if not cwd or not answer or not _NEW_LABEL.search(answer):
        return answer, 0
    try:
        from claude_hooks.findings import Ledger, findings_enabled
        from claude_hooks.findings.ledger import findings_root
        from consultants.engine import evidence
        if not findings_enabled():
            return answer, 0
        lg = Ledger(findings_root(cwd))
        if not lg.exists:
            return answer, 0
        lines = answer.split("\n")
        n = 0
        for it in evidence.ranked_items(answer):
            i = it["start"]
            if not _NEW_LABEL.search(lines[i]) or "harness: the ledger already" in lines[i]:
                continue
            f = lg.match(" ".join(it["text"].split()), paths_in(it["text"]))
            if f is None or f.id in it["text"]:
                continue
            note = "_(" + KNOWN_NOTE.format(id=f.id, status=f.status,
                                            title=f.title) + ")_"
            row = lines[i].rstrip()
            if row.lstrip().startswith("|"):
                cut = row[:-1].rstrip() if row.endswith("|") else row
                lines[i] = f"{cut} {note} |"
            else:
                lines[i] = f"{row} {note}"
            n += 1
        return "\n".join(lines), n
    except Exception:  # noqa: BLE001 — memory never fails a council
        log.exception("known-new check failed")
        return answer, 0


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
        lines = answer.split("\n")
        prefix = lg.prefix if lg.exists else ""
        out = []
        for it in items:
            cell = it.get("kind") == "cell"
            if cell and not _DEFECT_MARK.search(it.get("cell") or ""):
                continue      # ✓ is no defect; "not tested" has no mark left
            if cell:
                title = _cell_title(it)
                text = " ".join(evidence._claim_text(it).split())
                tag_in = f"{it.get('cell') or ''} {it['text']}"
            else:
                title = _title(it, lines)
                text = " ".join(it["text"].split())
                tag_in = it["text"]
            f, created = lg.record(
                title, text, paths=paths_in(it["text"]),
                severity="M" if cell else "H",
                evidence=_evidence(it), council=sid,
                reproduce=" ".join((it["runs"][0].get("command") or "").split()),
                known=_tag(tag_in, prefix) if prefix else "")
            out.append((f.id, created))
        return out
    except Exception:  # noqa: BLE001 — memory never fails a council
        log.exception("recording council %s findings failed", sid)
        return []


__all__ = ["with_known_findings", "record_answer", "findings_block",
           "known_findings", "paths_in", "settled_findings", "answer_items",
           "settled_audit_prompt", "parse_settled_audit",
           "apply_settled_audit", "SETTLED_AUDIT_SYSTEM",
           "examined_against", "EXAMINED_NOTE", "note_known_new"]
