"""Decision-model seams in the council (plan H).

One entry point, :func:`ask`, for every place the council consults a
decision model (``claude_hooks.decision_client``: TypeSafe's Jev or a
``/v1/systemone`` server). It reads ``runtime_control.decision`` (seeded
only when ``[decision] enabled``), checks the seam's switch, and never
raises: any failure — off, unreachable, refused, malformed — returns
``None`` and the caller keeps today's behaviour. Each call is recorded as a
``decision`` event with the seam, model, latency and answers.

Seams (``[decision] seams``):

``review``      :func:`review_answer` — accept / followup / research on the
                council's answer, carried to the consultancy review loop.
``confidence``  :func:`review_answer` too (same call, one more question):
                a scored confidence beside the synthesizer's self-rating;
                :func:`score_evidence` for the critic.
``struggle``    :func:`stuck` — is a lane repeating itself without new
                information?
``citations``   :func:`check_citations` — does the quoted code support the
                claim citing it?

A seam named in ``[decision] shadow`` is asked and recorded like any
other, but never acted on (:func:`shadowed`): no confidence on the
channel, no recommendation, no nudge, no citation note. That collects
live scores next to what the reviewer later said (plan H6) before any
seam is trusted. Shadow wins over ``seams``.

Every state part is bounded (``decision_client.bound_text``) so a long
answer cannot carry a whole transcript to the scorer.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from claude_hooks import decision_client as dc

log = logging.getLogger("consultants.engine.decision")

REVIEW_OPTIONS = {
    "accept": "the answer is specific, evidenced and complete for the "
              "question; accept it",
    "followup": "the answer is on the right track but misses or leaves "
                "unverified a specific part; ask a focused follow-up",
    "research": "the answer does not address the question; research again",
}


def settings(state: Optional[dict]) -> Optional[dict]:
    rc = (state or {}).get("runtime_control") or {}
    d = rc.get("decision")
    return d if isinstance(d, dict) and (d.get("seams") or d.get("shadow")) \
        else None


def enabled(state: Optional[dict], seam: str) -> bool:
    """The seam is asked: acting or in shadow."""
    d = settings(state)
    return bool(d) and (seam in (d.get("seams") or ())
                        or seam in (d.get("shadow") or ()))


def shadowed(state: Optional[dict], seam: str) -> bool:
    """The seam is asked and recorded, but its answer must not act."""
    d = settings(state)
    return bool(d) and seam in (d.get("shadow") or ())


def endpoint(d: dict) -> dc.Endpoint:
    return dc.Endpoint(
        base_url=d.get("url") or dc.DEFAULT_BASE_URL,
        model=d.get("model") or dc.DEFAULT_MODEL,
        key_file=d.get("key_file"),
        timeout_s=float(d.get("timeout_s") or dc.DEFAULT_TIMEOUT_S),
        floor=float(d.get("floor") or dc.DEFAULT_FLOOR),
        high_stakes_floor=float(d.get("high_stakes_floor")
                                or dc.DEFAULT_HIGH_STAKES_FLOOR))


def ask(state: Optional[dict], seam: str, decision_state: Any,
        questions: dict[str, dict], *, recorder=None, role: str = "",
        round: Optional[int] = None, lane_idx: Optional[int] = None,
        transport=None) -> Optional[dc.Response]:
    """Ask the configured decision model, or ``None`` (seam off / failed)."""
    if not enabled(state, seam):
        return None
    d = settings(state)
    ep = endpoint(d)
    error = None
    resp = None
    try:
        resp = dc.evaluate(ep, decision_state, questions, transport=transport)
    except dc.DecisionError as e:
        error = str(e)
        log.warning("decision %s (%s): %s", seam, ep.model, e)
    except Exception as e:  # pragma: no cover — never fail the council
        error = f"{type(e).__name__}: {e}"
        log.exception("decision %s raised", seam)
    _record(recorder, seam=seam, role=role, round=round, lane_idx=lane_idx,
            model=ep.model, resp=resp, error=error,
            shadow=tuple(d.get("shadow") or ()))
    return resp


def _record(recorder, *, seam, role, round, lane_idx, model, resp, error,
            shadow=()):
    if recorder is None or not hasattr(recorder, "record_event"):
        return
    payload = {"seam": seam, "model": model, "error": error}
    if shadow:
        payload["shadow"] = list(shadow)
    if resp is not None:
        payload.update(served_by=resp.model, dialect=resp.dialect,
                       duration_ms=resp.duration_ms, answers=resp.answers,
                       usage=resp.usage)
    try:
        recorder.record_event(kind="decision", role=role or seam,
                              round=round, lane_idx=lane_idx, payload=payload)
    except Exception:  # pragma: no cover
        log.debug("recorder.record_event raised; ignored", exc_info=True)


# --- seams ------------------------------------------------------------- #

def review_answer(state: dict, question: str, answer: str, *,
                  recorder=None, transport=None) -> dict:
    """The synthesizer's seams in one call: ``review`` (a recommendation)
    and ``confidence`` (p that the answer is right and complete). Returns
    ``{"recommendation": {...}?, "confidence": float?}`` — only what was
    asked and answered."""
    questions: dict[str, dict] = {}
    if enabled(state, "review"):
        questions["next"] = dc.choice(
            "What should the council do next with `answer` to `question`?",
            REVIEW_OPTIONS)
    if enabled(state, "confidence"):
        questions["sound"] = dc.noul(
            "Does `answer` correctly and completely answer `question`, "
            "with evidence for its conclusions?")
    if not questions:
        return {}
    resp = ask(state, "review" if "next" in questions else "confidence",
               {"question": dc.bound_text(question),
                "answer": dc.bound_text(answer)},
               questions, recorder=recorder, role="synthesizer",
               transport=transport)
    if resp is None:
        return {}
    d = settings(state) or {}
    floor = float(d.get("floor") or dc.DEFAULT_FLOOR)
    out: dict = {}
    nxt = resp.answers.get("next")
    if isinstance(nxt, dict) and nxt.get("choice") in REVIEW_OPTIONS \
            and not shadowed(state, "review"):
        conf = dc.confidence(nxt)
        out["recommendation"] = {
            "choice": nxt["choice"], "confidence": round(conf, 4),
            "confident": conf >= floor,
            "probabilities": {k: round(float(v), 4) for k, v in
                              (nxt.get("probabilities") or {}).items()},
            "model": resp.model}
    sound = resp.answers.get("sound")
    if isinstance(sound, dict) and "noul" in sound \
            and not shadowed(state, "confidence"):
        out["confidence"] = round(max(0.0, min(1.0, float(sound["noul"]))), 4)
    return out


def score_evidence(state: dict, question: str, evidence: str, *,
                   recorder=None, transport=None) -> Optional[float]:
    """The critic's confidence seam: p that ``evidence`` is sufficient and
    correct to answer ``question`` (what the critic's own
    ``CONFIDENCE:`` line rates)."""
    resp = ask(state, "confidence",
               {"question": dc.bound_text(question),
                "evidence": dc.bound_text(evidence)},
               {"enough": dc.noul(
                   "Is `evidence` sufficient and correct to answer "
                   "`question` without more research?")},
               recorder=recorder, role="critic", transport=transport)
    a = (resp.answers.get("enough") if resp else None) or {}
    if "noul" not in a:
        return None
    return round(max(0.0, min(1.0, float(a["noul"]))), 4)


def stuck(state: dict, goal: str, recent_calls: list[str], *,
          recorder=None, role: str = "researcher",
          lane_idx: Optional[int] = None,
          transport=None) -> Optional[dict]:
    """``{"p": float, "confident": bool}`` that the lane is stuck, or
    ``None``. Confident means p clears the high-stakes floor in the
    stuck direction: a nudge interrupts the lane, so it needs more than
    a coin flip."""
    if not recent_calls:
        return None
    resp = ask(state, "struggle",
               {"goal": dc.bound_text(goal, 2_000),
                "recent_calls": [dc.bound_text(c, 400)
                                 for c in recent_calls[-12:]]},
               {"stuck": dc.noul(
                   "Is the agent stuck, repeating itself without new "
                   "information, rather than making progress toward "
                   "`goal`?")},
               recorder=recorder, role=role, lane_idx=lane_idx,
               transport=transport)
    a = (resp.answers.get("stuck") if resp else None) or {}
    if "noul" not in a:
        return None
    p = max(0.0, min(1.0, float(a["noul"])))
    hs = float((settings(state) or {}).get("high_stakes_floor")
               or dc.DEFAULT_HIGH_STAKES_FLOOR)
    return {"p": round(p, 4), "confident": (2 * p - 1) >= hs}


def check_citations(state: dict, items: list[dict], *, recorder=None,
                    role: str = "synthesizer",
                    transport=None) -> dict[int, dict]:
    """For each ``{"claim", "code"}`` item, whether the code supports the
    claim. One call for all (Ollama takes ≤ 64 questions; the rest are
    left unchecked). Returns ``{index: {"p", "unsupported"}}`` where
    ``unsupported`` means confidently not supported at the floor."""
    items = [it for it in items if it.get("claim") and it.get("code")][:64]
    if not items:
        return {}
    state_obj = {f"c{i}": {"claim": dc.bound_text(it["claim"], 600),
                           "code": dc.bound_text(it["code"], 2_000)}
                 for i, it in enumerate(items)}
    questions = {f"q{i}": dc.noul(f"Does `c{i}.code` support `c{i}.claim`?")
                 for i in range(len(items))}
    resp = ask(state, "citations", state_obj, questions, recorder=recorder,
               role=role, transport=transport)
    if resp is None:
        return {}
    floor = float((settings(state) or {}).get("floor") or dc.DEFAULT_FLOOR)
    out: dict[int, dict] = {}
    for i in range(len(items)):
        a = resp.answers.get(f"q{i}") or {}
        if "noul" not in a:
            continue
        p = max(0.0, min(1.0, float(a["noul"])))
        out[i] = {"p": round(p, 4), "unsupported": (1 - 2 * p) >= floor}
    return out


__all__ = ["settings", "enabled", "endpoint", "ask", "review_answer",
           "score_evidence", "stuck", "check_citations", "REVIEW_OPTIONS"]
