"""What the council dashboard shows, read from the councils' own records.

Live councils come from the engine's sessions (status, per-role
progress, phase). Every council, live or past, comes from the host-wide
index (:mod:`consultants.server.council_index`), and its detail from
its own ``transcript.db``, opened read-only:

- **lanes**: one row per (role, round, lane) with its model, LLM calls,
  tokens, tool and command counts, time, and how it ended (plan D7);
- **timeline**: runtime events (node boundaries, injects, rewinds,
  checkpoints, guard trips) in event order;
- **commands**: the ``run_command`` calls, as the synthesizer is given
  them (:func:`consultants.engine.evidence.commands_ran`);
- **answer**: ``summary.md``, else the metadata's ``final_answer``.

Nothing here writes; a council whose directory is gone is ``missing``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from consultants.server import council_index

#: Statuses of a council that is still going.
LIVE = ("running", "paused", "awaiting_tool", "awaiting_adversary",
        "suspended", "queued")
TIMELINE_MAX = 2000
#: Per-call runtime events: the lanes table already counts them, so the
#: timeline leaves them out unless asked (``full``).
PER_CALL_KINDS = ("llm_call", "tool_call", "stall.attempt.ok",
                  "tool_approval_auto")
ANSWER_MAX_CHARS = 200_000


def _ro(db: Path) -> Optional[sqlite3.Connection]:
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _live_view(s) -> dict:
    out = {"status": getattr(s, "status", None),
           "progress": dict(getattr(s, "progress", {}) or {}),
           "consultancy_status": getattr(s, "consultancy_status", None)}
    # Waiting is the deadline window only. ``_adversary_checkpoint_active``
    # spans the whole runner-owned stretch (the /resume guard), rewind
    # included, so run 6 showed "awaiting adversary" with lanes running.
    deadline = getattr(s, "_checkpoint_deadline_ts", None)
    if deadline is not None:
        out["phase"] = "awaiting_adversary"
        out["checkpoint_deadline_ts"] = deadline
    rc = getattr(s, "run_control", None)
    if rc is not None:
        for k in ("paused", "cancelled", "wrapping_up", "suspended"):
            v = getattr(rc, k, None)
            if isinstance(v, bool) and v:
                out.setdefault("flags", []).append(k)
    return out


def _inflight_sids() -> set[str]:
    try:
        from consultants.server import inflight
        return {r.get("sid") for r in inflight.load()}
    except Exception:  # pragma: no cover — defensive
        return set()


def council_list(app, *, limit: int = 200, status: Optional[str] = None,
                 query: Optional[str] = None) -> dict:
    sessions = dict(getattr(app.state, "sessions", {}) or {})
    rows = council_index.list_councils(limit=limit, status=None, query=query)
    by_sid = {r["sid"]: r for r in rows}
    # A live session the index does not have yet (an engine that ran
    # before the index existed resumed it): list it all the same.
    for sid, s in sessions.items():
        if sid not in by_sid:
            by_sid[sid] = {"sid": sid, "cwd": getattr(s, "cwd", ""),
                           "question": (getattr(s, "question", "") or "")[:2000],
                           "effort": getattr(s, "effort", None),
                           "parent_sid": getattr(s, "parent_sid", None),
                           "root_sid": getattr(s, "root_sid", None) or sid,
                           "started_at": getattr(s, "started_at", None),
                           "status": getattr(s, "status", None),
                           "kind": "follow-up" if getattr(s, "parent_sid", None)
                           else "consult", "missing": False}
    inflight = _inflight_sids()
    now = time.time()
    out = []
    for sid, r in by_sid.items():
        r = dict(r)
        s = sessions.get(sid)
        if s is not None:
            r["live"] = _live_view(s)
            r["status"] = r["live"]["status"] or r.get("status")
        elif r.get("status") in LIVE and sid not in inflight:
            # Indexed as running, but no engine is running it and nothing
            # will resume it: the engine went away under it.
            r["status"] = "lost"
        end = r.get("finished_at") or (now if r.get("status") in LIVE else None)
        if r.get("started_at") and end:
            r["duration_s"] = round(end - r["started_at"], 1)
        out.append(r)
    if status:
        out = [r for r in out if r.get("status") == status]
    out.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return {"councils": out[:limit], "now": now,
            "running": sum(1 for r in out if r.get("status") in LIVE)}


def _lanes(con: sqlite3.Connection) -> list[dict]:
    lanes: dict[tuple, dict] = {}

    def lane(role, rnd, idx):
        key = (role or "?", rnd, idx)
        if key not in lanes:
            lanes[key] = {"role": key[0], "round": rnd, "lane_idx": idx,
                          "models": [], "llm_calls": 0, "prompt_tokens": 0,
                          "completion_tokens": 0, "tool_calls": 0,
                          "commands": 0, "errors": 0, "first_ts": None,
                          "last_ts": None}
        return lanes[key]

    for r in con.execute(
            "SELECT ts, kind, role, round, lane_idx, model, prompt_tokens, "
            "completion_tokens, tool, error FROM events ORDER BY event_id"):
        role = (r["role"] or "?").split(":")[0]
        ln = lane(role, r["round"], r["lane_idx"])
        ln["first_ts"] = ln["first_ts"] or r["ts"]
        ln["last_ts"] = r["ts"]
        if r["kind"] == "llm_call":
            ln["llm_calls"] += 1
            ln["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            ln["completion_tokens"] += int(r["completion_tokens"] or 0)
            if r["model"] and r["model"] not in ln["models"]:
                ln["models"].append(r["model"])
        elif r["kind"] == "tool_call":
            ln["tool_calls"] += 1
            if r["tool"] == "run_command":
                ln["commands"] += 1
        if r["error"]:
            ln["errors"] += 1
    for r in con.execute(
            "SELECT role, round, lane_idx, payload FROM runtime_events "
            "WHERE kind = 'node_exit' ORDER BY event_id"):
        try:
            body = json.loads(r["payload"] or "{}")
        except ValueError:
            body = {}
        ln = lane(r["role"], r["round"], r["lane_idx"])
        for k in ("stop_reason", "failure_class"):
            if body.get(k):
                ln[k] = body[k]
        if body.get("duration_ms") is not None:
            ln["duration_ms"] = body["duration_ms"]
    out = [v for v in lanes.values() if v["llm_calls"] or v["tool_calls"]
           or v.get("stop_reason")]
    for v in out:
        if v["first_ts"] and v["last_ts"]:
            v["span_s"] = round(v["last_ts"] - v["first_ts"], 1)
    order = {"planner": 0, "researcher": 1, "tool_executor": 2, "critic": 3,
             "meta_critic": 4, "coder": 5, "synthesizer": 6, "adversary": 7}
    out.sort(key=lambda v: (v["first_ts"] or 0, order.get(v["role"], 9)))
    return out


def _shadowed(kind: str, body: dict) -> bool:
    """A decision seam in ``[decision] shadow`` is asked and recorded but
    never shown: ``mine_decision_cases.py`` pairs those scores with what
    the reviewer did without seeing them, and a score on the dashboard
    would make the reviewer's choice no longer independent of it."""
    if kind != "decision":
        return False
    shadow = body.get("shadow")
    return bool(shadow) and (body.get("seam") in shadow if isinstance(
        shadow, (list, tuple)) else True)


def _timeline(con: sqlite3.Connection, since: int = 0, *,
              full: bool = False) -> list[dict]:
    out = []
    skip = () if full else PER_CALL_KINDS
    marks = ",".join("?" * len(skip)) or "''"
    for r in con.execute(
            "SELECT event_id, ts, kind, role, round, lane_idx, payload "
            f"FROM runtime_events WHERE event_id > ? AND kind NOT IN ({marks}) "
            "ORDER BY event_id LIMIT ?", (int(since), *skip, TIMELINE_MAX)):
        try:
            body = json.loads(r["payload"] or "{}")
        except ValueError:
            body = {}
        if _shadowed(r["kind"], body):
            continue
        for k in ("sid", "text", "messages"):
            body.pop(k, None)
        out.append({"id": r["event_id"], "ts": r["ts"], "kind": r["kind"],
                    "role": r["role"], "round": r["round"],
                    "lane_idx": r["lane_idx"],
                    "data": {k: (v if not isinstance(v, str) or len(v) < 400
                                 else v[:400] + "…") for k, v in body.items()}})
    return out


def _answer(sdir: Path) -> Optional[str]:
    p = sdir / "summary.md"
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")[:ANSWER_MAX_CHARS]
        meta = json.loads((sdir / "metadata.json").read_text(encoding="utf-8"))
        a = meta.get("final_answer")
        return a[:ANSWER_MAX_CHARS] if isinstance(a, str) else None
    except (OSError, ValueError):
        return None


def council_detail(app, sid: str, *, since: int = 0,
                   full: bool = False) -> Optional[dict]:
    row = council_index.get(sid)
    s = (getattr(app.state, "sessions", {}) or {}).get(sid)
    if row is None and s is None:
        return None
    row = dict(row or {"sid": sid, "cwd": getattr(s, "cwd", "")})
    sdir = council_index.session_dir(row["cwd"], sid)
    out: dict[str, Any] = {"council": row, "missing": not sdir.is_dir(),
                           "session_dir": str(sdir)}
    if s is not None:
        out["live"] = _live_view(s)
        # The index keeps a 2,000-char copy for the list; the detail shows
        # the council's own record, in full.
        if getattr(s, "question", None):
            row["question"] = s.question
    con = _ro(sdir / "transcript.db")
    if con is not None:
        try:
            out["lanes"] = _lanes(con)
            out["timeline"] = _timeline(con, since, full=full)
            meta = con.execute("SELECT * FROM meta LIMIT 1").fetchone()
            if meta is not None:
                m = dict(meta)
                try:
                    m["models"] = json.loads(m.pop("models_json") or "{}")
                except ValueError:
                    pass
                out["meta"] = m
                if m.get("question"):
                    row["question"] = m["question"]
            tok = con.execute(
                "SELECT COUNT(*), SUM(prompt_tokens), SUM(completion_tokens) "
                "FROM events WHERE kind = 'llm_call'").fetchone()
            out["totals"] = {"llm_calls": tok[0] or 0,
                             "prompt_tokens": tok[1] or 0,
                             "completion_tokens": tok[2] or 0}
        except sqlite3.Error as e:
            out["error"] = f"transcript.db: {e}"
        finally:
            con.close()
        from consultants.engine.evidence import commands_ran
        out["commands"] = commands_ran(sdir / "transcript.db") or []
    out["answer"] = _answer(sdir)
    try:
        out["sandbox"] = json.loads((sdir / "sandbox.json").read_text(
            encoding="utf-8")).get("lanes")
    except (OSError, ValueError):
        pass
    if row.get("root_sid"):
        out["chain"] = [r for r in council_index.list_councils(limit=100)
                        if r.get("root_sid") == row["root_sid"]][::-1]
    return out


# ------------------------------------------------- messages between roles
#: Typed role records (``[protocol] typed``), handed in as a tool call.
TYPED_RECORDS = ("council_plan", "council_report", "council_verdict",
                 "council_refutation")
MESSAGE_MAX_CHARS = 60_000
PREVIEW_CHARS = 700
CALL_MESSAGE_MAX_CHARS = 40_000
#: Runtime events shown as signals between the roles' messages.
SIGNAL_KINDS = ("inject", "rewind", "awaiting_adversary", "adversary_released",
                "adversary_ack",
                "decision", "guard", "truncation", "lane_retry",
                "lane_escalation", "council_complete", "wrap_up", "cancel",
                "infra_wait", "pause", "resume", "checkpoint")
_DECISION = re.compile(r"^\s*DECISION:\s*([a-z_]+)", re.I | re.M)
_CONF = re.compile(r"^\s*CONFIDENCE:\s*([0-9.]+)\s*$", re.I | re.M)


def _json(text: Optional[str]) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _response_message(resp: Any) -> dict:
    if not isinstance(resp, dict):
        return {}
    ch = resp.get("choices")
    if isinstance(ch, list) and ch:
        return (ch[0] or {}).get("message") or {}
    return resp.get("message") or {}


def _cut(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"\n\n…(cut: {len(text) - n:,} more characters)"


def _args_of(call: dict) -> Any:
    a = (call.get("function") or {}).get("arguments")
    if isinstance(a, str):
        v = _json(a)
        return v if v is not None else a
    return a


def _handed(req: Any) -> tuple[str, int, int]:
    """``(what the role was handed, its chars, system prompt chars)``
    from a request: the user turns before the first assistant turn."""
    msgs = (req or {}).get("messages") or [] if isinstance(req, dict) else []
    system = sum(len(str(m.get("content") or "")) for m in msgs
                 if m.get("role") == "system")
    given = []
    for m in msgs:
        if m.get("role") == "assistant":
            break
        if m.get("role") == "user":
            given.append(str(m.get("content") or ""))
    text = "\n\n".join(given)
    return text, len(text), system


def _recipients(role: str, roles: set[str], decision: Optional[str]) -> list[str]:
    after_research = (["critic"] if "critic" in roles else []) + ["synthesizer"]
    if role == "planner":
        return [r for r in ("researcher", "tool_executor", "coder") if r in roles] \
            or ["researcher"]
    if role in ("researcher", "tool_executor"):
        return after_research
    if role == "coder":
        return ["synthesizer"]
    if role == "critic" and "meta_critic" in roles:
        return ["meta_critic"]
    if role in ("critic", "meta_critic"):
        if decision == "needs_more_research":
            return ["researcher"]
        return ["synthesizer"]
    if role == "synthesizer":
        return ["user"] + (["adversary"] if "adversary" in roles else [])
    if role == "adversary":
        return ["user"]
    return []


def _signal_text(kind: str, role: Optional[str], lane, body: dict) -> str:
    who = (role or "") + (f" lane {lane}" if lane is not None else "")
    if kind == "inject":
        return (f"{body.get('source') or 'operator'} brief → "
                f"{body.get('target_role') or body.get('role') or '?'}: "
                f"{body.get('status') or ''}"
                + (f", routed {body['routed']}" if body.get("routed") else ""))
    if kind == "rewind":
        node = body.get("as_node") or "?"
        return ("rewound: the council goes back to "
                + ("research on the existing plan" if node == "rewind" else
                   "the planner" if node in ("__start__", "START") else node)
                + (f" ({body['reason']})" if body.get("reason") else ""))
    if kind == "awaiting_adversary":
        t = body.get("timeout_s")
        return ("adversary checkpoint opened"
                + (f", waits up to {int(t) // 60} min" if t else "")
                + (f" (synthesizer self-confidence {body['self_confidence']})"
                   if body.get("self_confidence") is not None else ""))
    if kind == "adversary_released":
        return {"ack": "adversary checkpoint released (acknowledged)",
                "timeout": "adversary checkpoint closed: no challenge in time",
                "suspended": "adversary checkpoint closed by an engine shutdown "
                             "(it reopens on resume)",
                "closed": "adversary checkpoint closed: the session ended",
                }.get(body.get("reason"), "adversary checkpoint closed")
    if kind == "decision":
        ans = body.get("answers") or {}
        bits = []
        for k, v in ans.items() if isinstance(ans, dict) else ():
            val = v.get(v.get("type")) if isinstance(v, dict) else v
            bits.append(f"{k} = {val}")
        return (f"decision model {body.get('model') or '?'} on {body.get('seam') or '?'}"
                + (f" for {role}" if role else "")
                + (": " + ", ".join(bits) if bits else "")
                + (" (shadow: recorded, not acted on)" if body.get("shadow") else "")
                + (f" — error: {body['error']}" if body.get("error") else ""))
    if kind == "guard":
        return (f"guard {body.get('guard') or '?'} on {who or '?'}: "
                f"{body.get('action') or ''} {body.get('kind') or ''}".rstrip()
                + (f" at iteration {body['iteration']}"
                   if body.get("iteration") is not None else ""))
    if kind == "truncation":
        return (f"{who or 'a role'}'s answer was cut ({body.get('kind') or '?'})"
                + (f": {body['detail']}" if body.get("detail") else ""))
    if kind in ("lane_retry", "lane_escalation"):
        return f"{kind.replace('_', ' ')}: " + ", ".join(
            f"{k} {v}" for k, v in body.items() if k not in ("kind", "sid", "ts")
            and not isinstance(v, (dict, list)))
    if kind == "council_complete":
        return f"council {body.get('status') or 'finished'}"
    return kind.replace("_", " ") + (f" ({who})" if who else "")


def council_messages(app, sid: str) -> Optional[dict]:
    """What the roles handed each other, in order: one ``message`` per
    role turn (its handoff, typed record, verdict, recipients and what it
    was given), and ``signal``s for the control events between them."""
    row = council_index.get(sid)
    s = (getattr(app.state, "sessions", {}) or {}).get(sid)
    if row is None and s is None:
        return None
    cwd = (row or {}).get("cwd") or getattr(s, "cwd", "")
    con = _ro(council_index.session_dir(cwd, sid) / "transcript.db")
    if con is None:
        return {"items": [], "missing": True}
    try:
        rows = con.execute(
            "SELECT event_id, ts, kind, role, round, lane_idx, model, "
            "response_json, prompt_tokens, completion_tokens, tool, "
            "duration_ms FROM events WHERE kind IN "
            "('node_enter', 'node_exit', 'llm_call', 'tool_call') "
            "ORDER BY event_id").fetchall()
        marks = ",".join("?" * len(SIGNAL_KINDS))
        sig_rows = con.execute(
            "SELECT event_id, ts, kind, role, round, lane_idx, payload FROM "
            f"runtime_events WHERE kind IN ({marks}) ORDER BY event_id",
            SIGNAL_KINDS).fetchall()
        exits = {}
        for r in con.execute("SELECT role, round, lane_idx, ts, payload FROM "
                             "runtime_events WHERE kind = 'node_exit'"):
            exits.setdefault((r["role"], r["lane_idx"]), []).append(
                (r["ts"], _json(r["payload"]) or {}))
    except sqlite3.Error as e:
        con.close()
        return {"items": [], "error": f"transcript.db: {e}"}

    segs: list[dict] = []
    open_: dict[tuple, dict] = {}

    def seg_for(role, rnd, lane, ts):
        key = (role, lane)
        if key not in open_:
            seg = {"role": role, "round": rnd, "lane": lane, "ts": ts,
                   "end_ts": None, "calls": [], "tools": 0, "models": [],
                   "prompt_tokens": 0, "completion_tokens": 0}
            open_[key] = seg
            segs.append(seg)
        return open_[key]

    for r in rows:
        full_role = r["role"] or "?"
        role = full_role.split(":")[0]
        key = (role, r["lane_idx"])
        if r["kind"] == "node_enter":
            open_.pop(key, None)
            seg_for(role, r["round"], r["lane_idx"], r["ts"])
        elif r["kind"] == "node_exit":
            seg = open_.pop(key, None)
            if seg is not None:
                seg["end_ts"] = r["ts"]
        elif r["kind"] == "tool_call":
            seg_for(role, r["round"], r["lane_idx"], r["ts"])["tools"] += 1
        else:
            seg = seg_for(role, r["round"], r["lane_idx"], r["ts"])
            seg["calls"].append({"id": r["event_id"], "ts": r["ts"],
                                 "phase": full_role.split(":", 1)[1]
                                 if ":" in full_role else None,
                                 "model": r["model"],
                                 "response": r["response_json"]})
            seg["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            seg["completion_tokens"] += int(r["completion_tokens"] or 0)
            if r["model"] and r["model"] not in seg["models"]:
                seg["models"].append(r["model"])

    roles = {sg["role"] for sg in segs}
    items: list[dict] = []
    for sg in segs:
        main = [c for c in sg["calls"] if not c["phase"]]
        if not main:
            continue
        out_text, typed = "", None
        for c in reversed(main):
            msg = _response_message(_json(c["response"]))
            for tc in msg.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name")
                if name in TYPED_RECORDS and typed is None:
                    typed = {"record": name, "fields": _args_of(tc)}
            if (msg.get("content") or "").strip() and not msg.get("tool_calls"):
                out_text = msg["content"]
                break
        if not out_text and typed is None:
            out_text = ""
        first = main[0]
        try:
            req = _json(con.execute("SELECT request_json FROM events WHERE "
                                    "event_id = ?", (first["id"],)).fetchone()[0])
        except (sqlite3.Error, TypeError):
            req = None
        given, given_chars, system_chars = _handed(req)
        dm = _DECISION.search(out_text or "")
        decision = dm.group(1).lower() if dm else None
        if typed and typed["record"] == "council_verdict" and isinstance(
                typed["fields"], dict):
            decision = str(typed["fields"].get("decision") or decision or "") or None
        cm = list(_CONF.finditer(out_text or ""))
        stop = None
        for ts, body in exits.get((sg["role"], sg["lane"]), []):
            if ts >= sg["ts"] and (sg["end_ts"] is None or ts <= sg["end_ts"] + 1):
                stop = {k: body.get(k) for k in ("stop_reason", "failure_class")
                        if body.get(k)}
                break
        items.append({
            "type": "message", "ts": sg["ts"], "end_ts": sg["end_ts"],
            "from": sg["role"], "round": sg["round"], "lane": sg["lane"],
            "models": sg["models"], "to": _recipients(sg["role"], roles, decision),
            "text": _cut(out_text, MESSAGE_MAX_CHARS), "typed": typed,
            "decision": decision,
            "confidence": float(cm[-1].group(1)) if cm else None,
            "given": {"call": first["id"], "chars": given_chars,
                      "system_chars": system_chars,
                      "preview": _cut(given, PREVIEW_CHARS)},
            "calls": [{"id": c["id"], "phase": c["phase"], "model": c["model"]}
                      for c in sg["calls"]],
            "tools": sg["tools"], "prompt_tokens": sg["prompt_tokens"],
            "completion_tokens": sg["completion_tokens"],
            "running": sg["end_ts"] is None and s is not None, **(stop or {})})
    con.close()
    for r in sig_rows:
        body = _json(r["payload"]) or {}
        if _shadowed(r["kind"], body):
            continue
        body.pop("sid", None)
        items.append({"type": "signal", "id": r["event_id"], "ts": r["ts"],
                      "kind": r["kind"], "role": r["role"], "lane": r["lane_idx"],
                      "text": _signal_text(r["kind"], r["role"], r["lane_idx"], body),
                      "data": {k: (_cut(v, 2000) if isinstance(v, str) else v)
                               for k, v in body.items()}})
    items.sort(key=lambda it: it["ts"] or 0)
    return {"items": items, "roles": sorted(roles)}


def call_detail(app, sid: str, event_id: int) -> Optional[dict]:
    """One LLM call in full: every message the role was sent (system,
    user, its own earlier turns and tool calls, tool results, harness
    notes) and what it answered."""
    row = council_index.get(sid)
    s = (getattr(app.state, "sessions", {}) or {}).get(sid)
    if row is None and s is None:
        return None
    cwd = (row or {}).get("cwd") or getattr(s, "cwd", "")
    con = _ro(council_index.session_dir(cwd, sid) / "transcript.db")
    if con is None:
        return None
    try:
        r = con.execute("SELECT event_id, ts, role, round, lane_idx, model, "
                        "request_json, response_json, prompt_tokens, "
                        "completion_tokens, duration_ms, error FROM events "
                        "WHERE event_id = ? AND kind = 'llm_call'",
                        (int(event_id),)).fetchone()
    except sqlite3.Error:
        r = None
    finally:
        con.close()
    if r is None:
        return None
    req = _json(r["request_json"]) or {}

    def msg_view(m: dict) -> dict:
        out = {"role": m.get("role"),
               "content": _cut(m.get("content") if isinstance(m.get("content"), str)
                               else json.dumps(m.get("content")) if m.get("content")
                               else "", CALL_MESSAGE_MAX_CHARS)}
        if m.get("_harness"):
            out["harness"] = True
        if m.get("thinking") or m.get("reasoning_content"):
            out["thinking"] = _cut(m.get("thinking") or m.get("reasoning_content"),
                                   CALL_MESSAGE_MAX_CHARS)
        if m.get("tool_calls"):
            out["tool_calls"] = [{"id": tc.get("id"),
                                  "name": (tc.get("function") or {}).get("name"),
                                  "args": _args_of(tc)} for tc in m["tool_calls"]]
        for k in ("tool_call_id", "name"):
            if m.get(k):
                out[k] = m[k]
        return out

    resp = _response_message(_json(r["response_json"]))
    return {"id": r["event_id"], "ts": r["ts"], "role": r["role"],
            "round": r["round"], "lane": r["lane_idx"], "model": r["model"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "duration_ms": r["duration_ms"], "error": r["error"],
            "tools_offered": [(t.get("function") or {}).get("name")
                              for t in req.get("tools") or [] if isinstance(t, dict)],
            "options": {k: req.get(k) for k in ("think", "reasoning_effort")
                        if req.get(k) is not None},
            "messages": [msg_view(m) for m in req.get("messages") or []
                         if isinstance(m, dict)],
            "response": msg_view(resp) if resp else None}


__all__ = ["council_list", "council_detail", "council_messages",
           "call_detail", "LIVE"]
