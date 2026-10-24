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
- **Roles send and receive the record as JSON** (plan G7, owner
  2026-10-19: as xOllama's council does; only the synthesizer's answer is
  prose). :func:`dumps` is what goes on the state channel: the record
  tagged with its tool name. Every engine reader takes the fields first
  (:func:`loads`, :func:`citations_of`, :func:`first_line`) and its prose
  regex only for text that is not a record (the prose fallback, typed
  off). :func:`render` is kept for display. xOllama measured critics
  answering in JSON once JSON was in their prompt, so :func:`loads` also
  takes an untagged JSON object that validates as the expected record.
- **Bounds:** item counts go in the schema, text length is cut at render.
  Never put ``maxLength`` in a schema: it made xOllama's grammar compiler
  fail and killed a run at 692 s (``55d4baf70``).
- **Degrade, never fail.** A record that is missing or doesn't parse
  leaves the prose path exactly as it was.
"""

from __future__ import annotations

import json
import logging
import re
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


# ------------------------------------------------------------ verification

#: Lines either side of a claim's ``line`` searched for its ``symbol`` and
#: its ``quote`` (xOllama's ``readShows`` window, ``quote.go``).
NEAR_LINES = 3


#: A comment marker leading a line. A quote that runs over two comment
#: lines is quoted without the second marker, so it is matched with the
#: markers dropped (G6: a true quote read as "not in the file").
_COMMENT_LEAD = re.compile(r"^\s*(?:#+|//+|/\*+|\*+/?|--|;+)[ \t]?")

#: A ``symbol`` that names code: an identifier, dotted, maybe called. Prose
#: such as "`X` comment" or "module docstring" names nothing to look up.
_IDENTIFIER = re.compile(r"[A-Za-z_$]\w*(?:(?:\.|::)[A-Za-z_$]\w*)*(?:\(.*\))?")


def _squash(text: str) -> str:
    text = "\n".join(_COMMENT_LEAD.sub("", ln)
                     for ln in str(text or "").splitlines())
    return " ".join(text.replace("`", "").split())


def _symbol_near(path, lines: list[str], symbol: str,
                 line: Optional[int]) -> bool:
    from consultants.engine import citation_linter as cl
    raw = symbol.strip().strip("`").strip()
    if not _IDENTIFIER.fullmatch(raw):
        return True
    sym = re.split(r"\.|::", raw.split("(")[0])[-1].strip()
    if not sym:
        return True
    if line is None:
        return bool(re.search(rf"\b{re.escape(sym)}\b", "\n".join(lines)))
    if cl._symbol_appears_in_range(path, sym, line, None, slack=NEAR_LINES):
        return True
    if str(path).endswith(".py"):
        return cl._enclosing_symbol_at_line(path, line) == sym
    return False


def _quote_at(lines: list[str], quote: str, line: Optional[int]
              ) -> tuple[bool, Optional[int]]:
    """``(near, found_at)``: whether the quote is within :data:`NEAR_LINES`
    of ``line`` (anywhere when there is no line), else the first line it
    starts on elsewhere, or ``None``."""
    q = _squash(quote)
    if not q:
        return True, None
    def window(lo, hi):
        return _squash("\n".join(lines[max(0, lo):hi]))
    if line is None:
        return (q in _squash("\n".join(lines))), None
    if q in window(line - 1 - NEAR_LINES, line + NEAR_LINES):
        return True, None
    span = max(1, len(quote.splitlines()))
    for i in range(len(lines)):          # the line the quote starts on
        if q in window(i, i + span):
            return False, i + 1
    return False, None


def verify_claims(rec: dict, allowed_roots) -> dict:
    """Plan G4: every ``council_report`` claim checked field by field at the
    source before it is rendered — the file exists under the roots, the
    line is inside it, the ``symbol`` is at (or encloses) that line and the
    ``quote`` is in the file there. A field that fails puts its reason in
    the claim's ``unverified``; nothing is dropped. Replaces the linter's
    proximity check for typed reports, which on rendered JSON went silent
    for any claim over ~65 characters."""
    from consultants.engine import citation_linter as cl
    roots = [r for r in (allowed_roots or ()) if r]
    if not roots:
        return rec
    out = []
    for c in rec.get("claims") or []:
        c = dict(c)
        notes: list[str] = []
        line = c.get("line")
        path = cl._resolve_path(c["path"], roots)
        if path is None:
            iss = cl.verify_citation(c["path"], line or 1, None,
                                     allowed_roots=roots)
            notes.append(iss.replacement.split("[unverified — ", 1)[-1]
                         .rstrip("]") if iss else "file not found")
        else:
            lines = cl._load_file_lines(path) or []
            if line is not None and line > len(lines):
                notes.append(f"file has {len(lines)} lines")
            else:
                if c.get("symbol") and not _symbol_near(path, lines,
                                                       c["symbol"], line):
                    notes.append(f"`{c['symbol']}` is not at that line")
                if c.get("quote"):
                    near, at = _quote_at(lines, c["quote"], line)
                    if not near:
                        notes.append(f"the quote is at line {at}" if at
                                     else "the quote is not in the file")
        if notes:
            c["unverified"] = "; ".join(notes)
        out.append(c)
    return {**rec, "claims": out}


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
            bad = (f" [unverified — {c['unverified']}]"
                   if c.get("unverified") else "")
            lines.append(f"- {c['claim']} (`{loc}`{bad}){sym}{quote}")
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


# --------------------------------------------------------------- G7 codec

#: The key that names a record's kind on the state channel.
RECORD_KEY = "record"
RESULT_NAMES = (PLAN, REPORT, VERDICT, REFUTATION)


def dumps(name: str, rec: dict) -> str:
    """The record as the next role receives it: JSON, tagged."""
    return json.dumps({RECORD_KEY: name, **rec}, ensure_ascii=False, indent=1)


def loads(text: Any, name: Optional[str] = None) -> Optional[dict]:
    """The record ``text`` carries (its fields, without the tag), or
    ``None`` when it is prose. With ``name``, only that kind: a tagged
    record of another kind is ``None``, and an untagged JSON object is
    taken when :func:`parse` validates it as ``name``."""
    s = text.strip() if isinstance(text, str) else ""
    if not s.startswith("{"):
        fence = re.match(r"^```(?:json)?\s*\n(\{.*\})\s*```$", s, re.S)
        if not fence:
            return None
        s = fence.group(1)
    try:
        # The leading object only: the engine appends notes after a
        # record (the evidence hold, the cross-examination block).
        d, _end = json.JSONDecoder().raw_decode(s)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    tag = d.get(RECORD_KEY)
    if tag in RESULT_NAMES:
        if name and tag != name:
            return None
        return {k: v for k, v in d.items() if k != RECORD_KEY}
    return parse(name, d) if name else None


def kind_of(text: Any) -> Optional[str]:
    """The record kind ``text`` carries, or ``None``."""
    for name in RESULT_NAMES:
        if loads(text, name) is not None:
            return name
    return None


def citations_of(text: Any) -> list[tuple[str, str, int, Optional[int]]]:
    """``extract_citations()`` for a record or prose: a report's claims give
    their ``path`` + ``line`` (JSON keeps them apart, so the prose regex
    finds none), its other fields and any cite strings are read as text."""
    from consultants.engine.citation_linter import extract_citations
    rec = None
    name = kind_of(text)
    if name:
        rec = loads(text, name)
    if rec is None:
        return extract_citations(str(text or ""))
    out = []
    for c in rec.get("claims") or []:
        if isinstance(c, dict) and c.get("path") and isinstance(c.get("line"), int):
            ref = f"{c['path']}:{c['line']}"
            out.append((ref, str(c["path"]), int(c["line"]), None))
    flat = json.dumps({k: v for k, v in rec.items()}, ensure_ascii=False)
    out += extract_citations(flat)
    seen, uniq = set(), []
    for t in out:
        if (t[1], t[2]) not in seen:
            seen.add((t[1], t[2]))
            uniq.append(t)
    return uniq


def first_line(text: Any) -> Optional[str]:
    """What a record says first (a report's summary, a verdict's decision
    and why, a plan's first step), or ``None`` for prose."""
    name = kind_of(text)
    if not name:
        return None
    rec = loads(text, name) or {}
    if name == REPORT:
        return str(rec.get("summary") or "")
    if name == VERDICT:
        return f"{rec.get('decision')}: {rec.get('why') or ''}".strip()
    if name == PLAN:
        steps = rec.get("steps") or []
        return str(steps[0]) if steps else ""
    issues = rec.get("issues") or []
    return f"{len(issues)} issue(s)" if issues else "none"

