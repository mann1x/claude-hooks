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
    if getattr(s, "_adversary_checkpoint_active", False):
        out["phase"] = "awaiting_adversary"
        out["checkpoint_deadline_ts"] = getattr(
            s, "_checkpoint_deadline_ts", None)
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


__all__ = ["council_list", "council_detail", "LIVE"]
