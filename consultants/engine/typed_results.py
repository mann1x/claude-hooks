"""Typed records between council roles (plan Milestone G).

xOllama's council moved inter-role messages from prose with markers to
typed records, and only the synthesizer still writes prose (report 07).
The same here, behind ``[protocol] typed`` (default off):

- **Carrier: a result tool the engine answers itself.** Each role gets
  one tool (``council_plan``, ``council_report``, ``council_verdict``,
  ``council_refutation``). Tool-call arguments travel on every backend.
  Grammar-constrained ``format`` does not: upstream docs say Ollama
  Cloud does not support structured outputs. The call is never
  executed. It ends the role's turn and its arguments are the record.
- **A record counts only as the turn's only call.** A result call made
  beside reads is not taken. The model is told to call it again on its
  own, so a report is never written before the reads it rests on.
- **Downstream roles read rendered text, never raw JSON.** The render
  reproduces exactly what each role writes in prose today (the
  ``DECISION:`` line, the ``CORRECTIONS:`` block, the ``REFUTATION:``
  block, ``CONFIDENCE:``, a numbered plan, bullets with literal
  ``path:line``). Routing, the escalator, the synthesizer, the
  retrospective and the recorder therefore work unchanged. xOllama
  measured critics answering in JSON once raw JSON was in their prompt.
- **Bounds:** item counts go in the schema, text length is cut at render.
  Never put ``maxLength`` in a schema: it made xOllama's grammar compiler
  fail and killed a run at 692 s (``55d4baf70``).
- **Degrade, never fail.** A record that is missing or doesn't parse
  leaves the prose path exactly as it was.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("consultants.engine.typed_results")

PLAN = "council_plan"
REPORT = "council_report"
VERDICT = "council_verdict"
REFUTATION = "council_refutation"

RESULT_TOOL_BY_ROLE = {
    "planner": PLAN,
    "researcher": REPORT,
    "critic": VERDICT,
    "meta_critic": VERDICT,
    "adversary": REFUTATION,
}

#: Render bounds (characters). Item counts are bounded in the schemas.
TEXT_MAX = 2000
FIELD_MAX = 600

_CITE = {"type": "string",
         "description": "`path:line` (or `path: symbol`) the item rests on"}

_SCHEMAS: dict[str, dict] = {
    PLAN: {
        "description": (
            "Hand the council its plan. Call this ONCE, on its own, when the "
            "plan is ready; it ends your turn. Each step is one sentence "
            "pointing at WHERE to look (file/path/symbol) or WHAT to verify."),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {"type": "array", "minItems": 1, "maxItems": 7,
                          "items": {"type": "string"}},
            },
            "required": ["steps"],
        },
    },
    REPORT: {
        "description": (
            "Hand in your research report. Call this ONCE, on its own, after "
            "your reads; it ends your turn. Every claim cites a `path` and "
            "`line` you saw in a tool result this turn. Put what you could "
            "not establish in `gaps`."),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "claims": {
                    "type": "array", "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                            "symbol": {"type": "string",
                                       "description": "the function, class "
                                       "or constant the claim is about"},
                            "quote": {"type": "string",
                                      "description": "a short exact excerpt "
                                      "of what the file has there"},
                        },
                        "required": ["claim", "path"],
                    },
                },
                "gaps": {"type": "array", "maxItems": 6,
                         "items": {"type": "string"}},
            },
            "required": ["summary", "claims"],
        },
    },
    VERDICT: {
        "description": (
            "Hand in your verdict. Call this ONCE, on its own, after any "
            "checks; it ends your turn. `decision` is `ready` unless you can "
            "name a concrete missing fact in `gaps`. A value you read that "
            "differs from a report goes in `corrections`, attributed to the "
            "report's number; a correction is not a reason to re-route."),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string",
                             "enum": ["ready", "needs_more_research"]},
                "why": {"type": "string"},
                "gaps": {
                    "type": "array", "maxItems": 3,
                    "items": {"type": "object",
                              "properties": {"what": {"type": "string"},
                                             "cite": _CITE},
                              "required": ["what"]},
                },
                "corrections": {
                    "type": "array", "maxItems": 12,
                    "items": {"type": "object",
                              "properties": {
                                  "report": {"type": "integer"},
                                  "claimed": {"type": "string"},
                                  "actual": {"type": "string"},
                                  "cite": _CITE},
                              "required": ["claimed", "actual"]},
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["decision", "why", "confidence"],
        },
    },
    REFUTATION: {
        "description": (
            "Hand in your refutation. Call this ONCE, on its own; it ends "
            "your turn. An empty `issues` list means every material claim "
            "is supported. Do not invent issues."),
        "parameters": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array", "maxItems": 10,
                    "items": {"type": "object",
                              "properties": {"claim": {"type": "string"},
                                             "why": {"type": "string"},
                                             "cite": _CITE},
                              "required": ["claim", "why"]},
                },
            },
            "required": ["issues"],
        },
    },
}


def result_tool_spec(name: str) -> dict:
    s = _SCHEMAS[name]
    return {"type": "function",
            "function": {"name": name, "description": s["description"],
                         "parameters": s["parameters"]}}


def result_tool_for(role: str) -> Optional[str]:
    return RESULT_TOOL_BY_ROLE.get(role)


#: Appended to a role's instructions when it carries its result tool.
#: Prose roles read rendered text, so none of them is ever told about
#: JSON at all; these roles are told the record is the reply.
def result_instruction(name: str) -> str:
    return (f"\n\nRESULT. End your turn by calling `{name}` once, on its own, "
            f"with your result. Its arguments ARE your reply: write nothing "
            f"else after it. If you cannot call tools, reply in plain text "
            f"in the format above instead.")


# ------------------------------------------------------------ parsing

def _args(raw: Any) -> Optional[dict]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return v if isinstance(v, dict) else None
    return None


def _s(v: Any, n: int = FIELD_MAX) -> str:
    text = " ".join(str(v or "").split()) if not isinstance(v, str) else v.strip()
    if len(text) > n:
        text = text[: n - 1].rstrip() + "…"
    return text


def _list(v: Any, n: int) -> list:
    return list(v)[:n] if isinstance(v, list) else []


def parse(name: str, raw_args: Any) -> Optional[dict]:
    """The validated record, or ``None`` when the arguments are not one.
    Unknown fields are dropped; counts are clamped to the schema's."""
    a = _args(raw_args)
    if a is None:
        return None
    try:
        if name == PLAN:
            steps = [_s(x) for x in _list(a.get("steps"), 7) if _s(x)]
            return {"steps": steps} if steps else None
        if name == REPORT:
            claims = []
            for c in _list(a.get("claims"), 16):
                if not isinstance(c, dict) or not _s(c.get("claim")) \
                        or not _s(c.get("path"), 300):
                    continue
                item = {"claim": _s(c["claim"]), "path": _s(c["path"], 300)}
                line = c.get("line")
                if isinstance(line, (int, float)) and int(line) > 0:
                    item["line"] = int(line)
                for k in ("symbol", "quote"):
                    if _s(c.get(k), 300):
                        item[k] = _s(c[k], 300)
                claims.append(item)
            summary = _s(a.get("summary"), TEXT_MAX)
            if not summary and not claims:
                return None
            return {"summary": summary, "claims": claims,
                    "gaps": [_s(g) for g in _list(a.get("gaps"), 6) if _s(g)]}
        if name == VERDICT:
            decision = str(a.get("decision") or "").strip().lower()
            if decision not in ("ready", "needs_more_research"):
                return None
            conf = a.get("confidence")
            try:
                conf = max(0.0, min(1.0, float(conf)))
            except (TypeError, ValueError):
                conf = None
            gaps = [{"what": _s(g.get("what")),
                     **({"cite": _s(g["cite"], 300)} if _s(g.get("cite"), 300) else {})}
                    for g in _list(a.get("gaps"), 3)
                    if isinstance(g, dict) and _s(g.get("what"))]
            corrections = []
            for c in _list(a.get("corrections"), 12):
                if not isinstance(c, dict) or not _s(c.get("claimed")) \
                        or not _s(c.get("actual")):
                    continue
                item = {"claimed": _s(c["claimed"]), "actual": _s(c["actual"])}
                if isinstance(c.get("report"), (int, float)):
                    item["report"] = int(c["report"])
                if _s(c.get("cite"), 300):
                    item["cite"] = _s(c["cite"], 300)
                corrections.append(item)
            return {"decision": decision, "why": _s(a.get("why"), TEXT_MAX),
                    "gaps": gaps, "corrections": corrections,
                    "confidence": conf}
        if name == REFUTATION:
            if not isinstance(a.get("issues"), list):
                return None
            issues = [{"claim": _s(i.get("claim")), "why": _s(i.get("why")),
                       **({"cite": _s(i["cite"], 300)} if _s(i.get("cite"), 300) else {})}
                      for i in _list(a.get("issues"), 10)
                      if isinstance(i, dict) and _s(i.get("claim"))]
            return {"issues": issues}
    except Exception:  # pragma: no cover — a record must never sink a run
        log.exception("typed_results: %s record failed to parse", name)
    return None


def take(name: str, response: dict) -> tuple[Optional[dict], str]:
    """``(record, status)`` from one response. ``status`` is ``taken``,
    ``absent`` (no result call), ``mixed`` (beside other calls — not
    taken), ``invalid`` or ``cut`` (arguments don't parse; ``cut`` when
    the reply hit its output limit)."""
    choices = (response or {}).get("choices") or [{}]
    ch = choices[0] or {}
    calls = (ch.get("message") or {}).get("tool_calls") or []
    mine = [c for c in calls if ((c.get("function") or {}).get("name") == name)]
    if not mine:
        return None, "absent"
    if len(calls) > 1:
        return None, "mixed"
    rec = parse(name, (mine[0].get("function") or {}).get("arguments"))
    if rec is not None:
        return rec, "taken"
    cut = (ch.get("done_reason") or response.get("done_reason")) == "length"
    return None, ("cut" if cut else "invalid")


#: Sent back when a record cannot be taken. Each names what to do.
NOT_ALONE = ("Not taken yet: call {name} on its own, in a turn of its own, "
             "after you have read what your other calls returned.")
CUT_OR_INVALID = ("Your {name} call was {what}. Call {name} again, smaller: "
                  "shorter fields and fewer items. Nothing else.")


# ------------------------------------------------------------ rendering

def _cite(c: Optional[str]) -> str:
    return f" (`{c}`)" if c else ""


def render(name: str, rec: dict) -> str:
    """The record as the prose its role writes today."""
    if name == PLAN:
        return "\n".join(f"{i}. {s}" for i, s in enumerate(rec["steps"], 1))
    if name == REPORT:
        lines = [rec["summary"]] if rec.get("summary") else []
        for c in rec.get("claims") or []:
            loc = f"{c['path']}:{c['line']}" if c.get("line") else c["path"]
            sym = f" [`{c['symbol']}`]" if c.get("symbol") else ""
            quote = f" — reads: `{c['quote']}`" if c.get("quote") else ""
            lines.append(f"- {c['claim']} (`{loc}`){sym}{quote}")
        gaps = rec.get("gaps") or []
        if gaps:
            lines.append("Evidence insufficient:")
            lines.extend(f"- {g}" for g in gaps)
        return "\n".join(lines).strip()
    if name == VERDICT:
        lines = [f"DECISION: {rec['decision']}"]
        if rec.get("why"):
            lines.append(rec["why"])
        for g in rec.get("gaps") or []:
            # ``gap_named:`` is the marker the xauto escalator reads.
            lines.append(f"- gap_named: {g['what']}{_cite(g.get('cite'))}")
        corr = rec.get("corrections") or []
        if corr:
            lines.append("CORRECTIONS:")
            for c in corr:
                who = f"report {c['report']}" if c.get("report") else "report"
                lines.append(f"- {who}: claimed {c['claimed']} — actual "
                             f"{c['actual']}{_cite(c.get('cite'))}")
        if rec.get("confidence") is not None:
            lines.append(f"CONFIDENCE: {rec['confidence']:.2f}")
        return "\n".join(lines)
    if name == REFUTATION:
        issues = rec.get("issues") or []
        if not issues:
            return "REFUTATION: none"
        return "REFUTATION:\n" + "\n".join(
            f"- {i['claim']} — {i['why']}{_cite(i.get('cite'))}" for i in issues)
    raise KeyError(name)
