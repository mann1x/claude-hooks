"""Cross-examination (docs/PLAN-council-memory.md, M5).

A critic that is not convinced used to send the whole round back with a
blanket ``needs_more_research``, and the next round re-did the research
without ever being asked about the claim the critic doubted. With
``[critic] cross_examination = on`` the critic names each doubt:

    CHALLENGE report=3: S2e calls the edit of line 5 BLIND, but C58 shows
    line 5 read and unchanged. Settle with: a run that edits a read,
    unchanged line and shows whether it is refused.

- the lane that wrote report N resumes its own conversation (its last
  ``llm_call`` in ``transcript.db``, :func:`recorder.load_lane_thread`)
  with the challenges as a harness note, and answers with a run or a
  concession: one exchange, its own tools and view, a bounded loop;
  challenged lanes run in parallel (one ``Send`` each);
- the critic rules on each, about the claim: ``stands`` (the answer
  shows it), ``narrowed`` (part of it stands, restated), ``falls`` (the
  lane conceded, or its answer does not show it) or ``open``; a
  challenge with no ruling is open. The lane's own word caps the
  ruling: a claim it conceded cannot stand, nor one it narrowed stand
  whole. Council run 9 ruled with ``upheld`` / ``withdrawn``, and the
  critic read "upheld" as "the claim is upheld" both times — the
  opposite of what it meant;
- any open challenge falls back to the reroute, so nothing is lost; with
  none open the critic's own DECISION line decides.

The report → lane map is ``research_lanes``, filled beside ``research``
by the researcher wrapper; when the two lists disagree in length the
critic's verdict routes as before.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Lanes cross-examined per critic round, and challenges per lane.
MAX_LANES = 4
MAX_PER_LANE = 3
#: The challenged lane's loop: one exchange, room for a run or two.
LANE_ITERS = (12, 10)

STANDS, NARROWED, FALLS, OPEN = "stands", "narrowed", "falls", "open"
RULINGS = (STANDS, NARROWED, FALLS, OPEN)
#: What a lane may answer, and the best ruling each allows.
SETTLED, CONCEDED = "settled", "conceded"
_CAP = {CONCEDED: FALLS, NARROWED: NARROWED}
_RANK = {FALLS: 0, OPEN: 1, NARROWED: 2, STANDS: 3}

#: Appended to the critic's and meta-critic's prompt when it is on.
CRITIC_NOTE = (
    "CROSS-EXAMINATION is on. When a specific claim in a report does not "
    "convince you, challenge it instead of sending the whole round back: "
    "one line per claim, starting at the line's beginning,\n"
    "CHALLENGE report=<N>: <the claim, and why you doubt it>. Settle "
    "with: <the run or reading that would settle it>\n"
    "<N> is the report's number above. The lane that wrote it answers in "
    "its own conversation, with a run or a concession, before you rule. "
    "A report that re-raises a finding the ledger records as refuted or "
    "by_design: challenge it against that decision. A gap no lane has "
    "looked at yet is not a challenge: name it as before. With "
    "challenges, write DECISION: needs_more_research.")

LANE_NOTE = (
    "The critic challenges claims in your report. This is one exchange, "
    "not a new round: for each challenge either run what settles it and "
    "cite what it printed, narrow the claim to the part your runs show, "
    "or concede it plainly. Do not redo your research. End with one "
    "block per challenge, <N> being the number in its CHALLENGE line:\n"
    "ANSWER report=<N>: settled | narrowed | conceded — <what the run "
    "showed; the claim as narrowed; or what you withdraw>")

RULING_SYSTEM = (
    "You are the council's critic, ruling on your own challenges. Each "
    "challenged lane has answered in its own conversation. Rule on every "
    "challenge, one line each, at the line's beginning:\n"
    "RULING report=<N>: stands | narrowed | falls | open — <why>\n"
    "Each word is about the CLAIM. stands: the answer shows the claim "
    "as written, citing a run or the code that shows it. narrowed: part "
    "of it is shown — restate the part that stands. falls: the lane "
    "conceded it, or its answer does not show it. open: the answer "
    "neither shows nor gives it up. A claim the lane conceded falls; one "
    "it narrowed is at most narrowed. Then one line\n"
    "DECISION: ready | needs_more_research\n"
    "ready only when nothing is open and nothing else you doubted still "
    "needs research.")

BLOCK_HEADER = (
    "CROSS-EXAMINATION (the critic challenged claims in the reports; each "
    "lane answered in its own conversation, then the critic ruled on the "
    "claim). FALLS: do not rank it. NARROWED: rank only the part the "
    "ruling restates. STANDS: the answer showed it. OPEN: unresolved.")

_LEAD = r"^[ \t]*(?:[-*][ \t]*)?\**"
_CHALLENGE = re.compile(
    _LEAD + r"CHALLENGE\**[ \t]+report[ \t]*=?[ \t]*(\d+)\**[ \t]*[:—–-][ \t]*",
    re.IGNORECASE | re.MULTILINE)
_RULING = re.compile(
    _LEAD + r"RULING\**[ \t]+report[ \t]*=?[ \t]*(\d+)\**[ \t]*[:—–-][ \t]*"
    r"\**(stands|narrowed|falls|open)\b\**[ \t]*[:—–-]?[ \t]*(.*)",
    re.IGNORECASE | re.MULTILINE)
_ANSWER = re.compile(
    _LEAD + r"ANSWER\**[ \t]+report[ \t]*=?[ \t]*(\d+)\b[^:\n]*?[:—–-][ \t]*"
    r"\**(settled|narrowed|conceded)\b", re.IGNORECASE | re.MULTILINE)
_STOP = re.compile(_LEAD + r"(?:CHALLENGE|RULING|DECISION|CONFIDENCE)\b|^#{1,6}\s",
                   re.IGNORECASE | re.MULTILINE)


def challenges(text: str, n_reports: int) -> dict[int, list[str]]:
    """``{report: [challenge, ...]}`` in the order the critic named them;
    a report number outside 1..``n_reports`` is dropped, and so is a
    challenge past :data:`MAX_PER_LANE` for its report."""
    out: dict[int, list[str]] = {}
    text = text or ""
    for m in _CHALLENGE.finditer(text):
        n = int(m.group(1))
        stop = _STOP.search(text, m.end())
        body = text[m.end():stop.start() if stop else len(text)].strip()
        if not body or not 1 <= n <= n_reports:
            continue
        got = out.setdefault(n, [])
        if len(got) < MAX_PER_LANE:
            got.append(body)
    return out


def plan(state: dict) -> list[dict]:
    """One exam per challenged lane: ``{lane, reports: {N: [challenge]}}``,
    at most :data:`MAX_LANES`. [] when the critic challenged nothing, the
    report → lane map is missing or does not line up, or this round was
    already cross-examined."""
    if (state.get("critic_decision") or "ready") != "needs_more_research":
        return []
    rounds = int(state.get("research_rounds_used") or 0)
    if int(state.get("cross_exam_round") or 0) >= max(rounds, 1):
        return []
    research = list(state.get("research") or [])
    lanes = list(state.get("research_lanes") or [])
    if not research or len(lanes) != len(research):
        return []
    named = challenges(str(state.get("critique") or ""), len(research))
    exams: dict[Any, dict] = {}
    for n, items in named.items():
        tag = lanes[n - 1] if isinstance(lanes[n - 1], dict) else {}
        lane = tag.get("lane")
        key = "solo" if lane is None else lane
        if key not in exams:
            if len(exams) >= MAX_LANES:
                continue
            exams[key] = {"lane": lane, "reports": {}}
        exams[key]["reports"][n] = items
    return list(exams.values())


def lane_note(reports: dict[int, list[str]],
              tools: Optional[list[str]] = None) -> str:
    """The challenges, and the tools this exchange really has: council
    run 9's lane 2 had ``run_command`` offered and said it had none (its
    own earlier report said so after a cut turn), so it could only
    concede."""
    lines = [LANE_NOTE, ""]
    for n, items in sorted(reports.items()):
        for c in items:
            lines.append(f"CHALLENGE report={n}: {c}")
    nums = ", ".join(f"report={n}" for n in sorted(reports))
    lines.append(f"\nAnswer with {nums} — the numbers above, whatever "
                 "your report calls its items.")
    if tools:
        line = "Your tools in this exchange: " + ", ".join(tools) + "."
        if "run_command" in tools:
            line += (" run_command is among them, whatever an earlier turn "
                     "of yours said: a run is how a challenge is settled.")
        lines.append(line)
    return "\n".join(lines)


def lane_answers(text: str) -> dict[int, str]:
    """``{report: settled | narrowed | conceded}`` from a lane's answer;
    the last answer for a report wins."""
    return {int(m.group(1)): m.group(2).lower()
            for m in _ANSWER.finditer(text or "")}


def rulings(text: str) -> dict[int, tuple[str, str]]:
    """``{report: (ruling, why)}``; the last ruling for a report wins."""
    return {int(m.group(1)): (m.group(2).lower(), m.group(3).strip())
            for m in _RULING.finditer(text or "")}


def outcome(exams: list[dict], ruled: dict[int, tuple[str, str]]) -> dict[int, str]:
    """Each challenged report's ruling; open when the critic gave none or
    the lane gave no answer (unless the ruling is that the claim falls),
    and never better than what the lane itself said of its claim."""
    out: dict[int, str] = {}
    for e in exams:
        text = (e.get("answer") or "").strip()
        answered = bool(text) and e.get("ok", True)
        said = lane_answers(text) if answered else {}
        for n in e.get("reports") or {}:
            r = ruled.get(int(n), (OPEN, ""))[0]
            if not answered and r != FALLS:
                r = OPEN
            cap = _CAP.get(said.get(int(n), ""))
            if cap and _RANK[r] > _RANK[cap]:
                r = cap
            out[int(n)] = r
    return out


def records(exams: list[dict], ruled: dict[int, tuple[str, str]],
            decided: dict[int, str], rnd: int) -> list[dict]:
    """One ``cross_exam_rulings`` entry per challenged report: what the
    synthesis audits read (a claim narrowed against a settled finding has
    had its run against that reason)."""
    out = []
    for e in exams:
        for n, items in sorted((e.get("reports") or {}).items()):
            out.append({"round": rnd, "report": int(n), "lane": e.get("lane"),
                        "ruling": decided.get(int(n), OPEN),
                        "why": ruled.get(int(n), ("", ""))[1],
                        "challenges": list(items),
                        "answer": (e.get("answer") or "") if e.get("ok", True) else ""})
    return out


def ruling_messages(question: str, critique: str, exams: list[dict]) -> list[dict]:
    parts = [f"USER QUESTION:\n{question.strip()}",
             f"\nYOUR VERDICT, WITH THE CHALLENGES:\n{critique.strip()}"]
    for e in exams:
        lane = "solo" if e.get("lane") is None else e["lane"]
        for n, items in sorted((e.get("reports") or {}).items()):
            parts.append(f"\nCHALLENGES TO REPORT {n} (lane {lane}):\n"
                         + "\n".join(f"- {c}" for c in items))
        answer = (e.get("answer") or "").strip()
        parts.append(f"\nLANE {lane}'S ANSWER:\n"
                     + (answer if answer and e.get("ok", True)
                        else f"(no answer: {e.get('error') or 'the lane did not reply'})"))
    parts.append("\nRule on every challenge, then the DECISION line.")
    return [{"role": "system", "content": RULING_SYSTEM},
            {"role": "user", "content": "\n".join(parts)}]


def render(exams: list[dict], ruled: dict[int, tuple[str, str]],
           decided: dict[int, str]) -> str:
    """The block appended to the critique, every lane answer whole."""
    out = [BLOCK_HEADER]
    for e in exams:
        lane = "solo" if e.get("lane") is None else e["lane"]
        for n, items in sorted((e.get("reports") or {}).items()):
            why = ruled.get(int(n), ("", ""))[1]
            out.append(f"\n- report {n} (lane {lane}): {decided.get(int(n), OPEN).upper()}"
                       + (f" — {why}" if why else ""))
            out.extend(f"  challenge: {c}" for c in items)
        answer = (e.get("answer") or "").strip()
        out.append(f"  lane {lane}'s answer:\n"
                   + (answer if answer and e.get("ok", True)
                      else f"(none: {e.get('error') or 'no reply'})"))
    return "\n".join(out)


# ----------------------------- nodes ------------------------------ #

def examine_node(state: dict, *, chat_client, recorder=None,
                 tool_specs: Optional[list[dict]] = None, tool_executor=None,
                 cwd: str = "", think: Any = True) -> dict:
    """One challenged lane answers in its own conversation. Never raises:
    a lane that cannot answer leaves its challenges open."""
    from consultants.engine import council
    from consultants.engine.recorder import load_lane_thread
    from consultants.engine.storage import RoleTurn
    from claude_hooks.agent_loop.runner import harness_note
    exam = dict(state.get("cross_exam_item") or {})
    lane = exam.get("lane")
    rnd = max(int(state.get("research_rounds_used") or 0), 1)
    got = load_lane_thread(getattr(recorder, "db_path", None), "researcher", lane)
    if got is None:
        return {"cross_exam": [{**exam, "ok": False,
                                "error": "the lane's conversation could not be read"}]}
    model, thread = got
    model = model or str(state.get("model_override") or "")
    msgs = [m for m in thread if isinstance(m, dict)]
    names = [str((s.get("function") or {}).get("name") or "")
             for s in tool_specs or ()] if tool_executor is not None else []
    msgs.append(harness_note(lane_note(exam.get("reports") or {},
                                       [n for n in names if n])))
    try:
        text, pt, ct = council._role_turn(
            chat_client, model, msgs, think=think, recorder=recorder,
            role="researcher:cross_exam", round=rnd, lane_idx=lane,
            tool_specs=tool_specs, tool_executor=tool_executor, cwd=cwd,
            iters=LANE_ITERS)
    except Exception as e:  # noqa: BLE001 — an open challenge, not a failed run
        log.exception("cross-examination of lane %s failed", lane)
        return {"cross_exam": [{**exam, "ok": False,
                                "error": f"{type(e).__name__}: {e}"}]}
    turn = RoleTurn(role="researcher", round=rnd, content=text,
                    prompt_tokens=pt, completion_tokens=ct, duration_seconds=0.0)
    return {"cross_exam": [{**exam, "ok": True, "model": model, "answer": text}],
            "turns": [turn], "total_prompt_tokens": pt,
            "total_completion_tokens": ct}


def ruling_node(state: dict, *, chat_client, model: str, recorder=None,
                think: Any = True, shell: bool = False) -> dict:
    """The critic rules on its challenges; the route after it is the
    critic's. Never raises: a failed ruling leaves every challenge open,
    and the critic's own needs_more_research stands."""
    from consultants.engine import council
    from consultants.engine.storage import RoleTurn
    rounds = max(int(state.get("research_rounds_used") or 0), 1)
    exams = [e for e in state.get("cross_exam") or []
             if int(e.get("round") or 0) == rounds]
    critique = str(state.get("critique") or "")
    done = {"cross_exam_round": rounds}
    if not exams:
        return done
    msgs = ruling_messages(str(state.get("question") or ""), critique, exams)
    if shell:
        msgs = council._with_command_ledger(msgs, recorder, chat_client=chat_client,
                                            model=model, state=state, role="critic")
    try:
        text, pt, ct = council._role_turn(
            chat_client, model, msgs, think=think, recorder=recorder,
            role="critic:ruling", round=rounds)
    except Exception as e:  # noqa: BLE001
        log.exception("cross-examination ruling failed")
        text, pt, ct = f"(ruling failed: {e})", 0, 0
    ruled = rulings(text)
    decided = outcome(exams, ruled)
    if any(r == OPEN for r in decided.values()):
        decision = "needs_more_research"
    else:
        decision = council.parse_critic_decision(text)
        if shell and decision == "ready":
            from consultants.engine.evidence import (
                commands_ran, hold_to_evidence_bar, round_lanes)
            _db = getattr(recorder, "db_path", None)
            decision, text, _held = hold_to_evidence_bar(
                decision, text, commands_ran(_db), round_lanes(_db))
    block = render(exams, ruled, decided)
    recs = records(exams, ruled, decided, rounds)
    turn = RoleTurn(role="critic", round=rounds, content=text,
                    prompt_tokens=pt, completion_tokens=ct, duration_seconds=0.0)
    return {**done,
            "cross_exam_rulings": recs,
            "critique": critique.rstrip() + "\n\n" + block,
            "critic_decision": decision,
            # The critic counted a reroute it may no longer make.
            "critic_reroutes_used": -1 if decision != "needs_more_research" else 0,
            "turns": [turn], "total_prompt_tokens": pt,
            "total_completion_tokens": ct}


__all__ = ["CRITIC_NOTE", "LANE_NOTE", "RULING_SYSTEM", "BLOCK_HEADER",
           "challenges", "plan", "lane_note", "lane_answers", "rulings",
           "outcome", "records", "STANDS", "NARROWED", "FALLS", "OPEN",
           "ruling_messages", "render", "examine_node", "ruling_node",
           "MAX_LANES", "MAX_PER_LANE", "LANE_ITERS"]
