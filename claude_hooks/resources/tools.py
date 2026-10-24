"""The resource MCP tools, on the memory servers every session already has.

A tool call cannot block for the hours a GPU may stay busy, so waiting is
the CLI's job: ``resource-acquire`` with ``queue: true`` books the claim
and answers with the exact background command (``claude-hooks-res wait
--claim <id>``) whose exit is the session's notification. Everything else
— status, release, check, definitions, history — is here.
"""
from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.resources import model
from claude_hooks.resources.model import Claim, SpecError

log = logging.getLogger("claude_hooks.resources.tools")

TOOL_NAMES = ("resource-status", "resource-acquire", "resource-release",
              "resource-check", "resource-define", "resource-events")

_SPECS = {"type": "array", "items": {"type": "string"},
          "description": "RES[:MODE[:UNITS]] each, e.g. solidpc/gpu0, "
                         "solidpc:quiet, ollama-cloud:shared:2. MODE is "
                         "shared, exclusive (default) or quiet. All of them "
                         "form one claim, granted together or not at all."}


def tool_catalog() -> list[dict]:
    return [
        {"name": "resource-status",
         "description": (
             "Who holds which host / GPU / pod / cloud resource and who is "
             "queued for what, across every session and host. Check it "
             "before using a shared machine instead of asking by mail."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "resource-acquire",
         "description": (
             "Claim resources before using them (a GPU for a run, a host "
             "kept quiet for a performance measurement, units of the cloud "
             "account). Granted now, or BUSY naming the holder. With "
             "queue=true a busy claim stays booked and the answer gives the "
             "command to run in the background: it exits the moment the "
             "claim is granted. A claim lasts until resource-release, its "
             "ttl, or the end of this session."),
         "inputSchema": {"type": "object", "properties": {
             "resources": _SPECS,
             "reason": {"type": "string",
                        "description": "what it is for; shown to others"},
             "ttl": {"type": "string",
                     "description": "end it after this long, e.g. 3h"},
             "queue": {"type": "boolean",
                       "description": "keep the booking when busy"}},
             "required": ["resources", "reason"]}},
        {"name": "resource-release",
         "description": (
             "Release this session's claims: by claim id, by resource name, "
             "or all. Release as soon as the work is done — waiters are "
             "woken at once."),
         "inputSchema": {"type": "object", "properties": {
             "claims": {"type": "array", "items": {"type": "string"}},
             "resources": {"type": "array", "items": {"type": "string"}},
             "all": {"type": "boolean"}}}},
        {"name": "resource-check",
         "description": "Would these resources be granted now? Books "
                        "nothing.",
         "inputSchema": {"type": "object", "properties": {
             "resources": _SPECS}, "required": ["resources"]}},
        {"name": "resource-define",
         "description": (
             "Give a resource a capacity (shared units, e.g. ollama-cloud "
             "= 3 connections) and/or a description. capacity 0 removes the "
             "definition."),
         "inputSchema": {"type": "object", "properties": {
             "name": {"type": "string"},
             "capacity": {"type": "integer"},
             "description": {"type": "string"}},
             "required": ["name"]}},
        {"name": "resource-events",
         "description": "Recent grants, releases, expiries and holder "
                        "exits.",
         "inputSchema": {"type": "object", "properties": {
             "limit": {"type": "integer"}}}},
    ]


def _line(c: Claim) -> str:
    since = (c.granted_at or c.created_at)[:19].replace("T", " ")
    ttl = (f", until {c.expires_at[:19].replace('T', ' ')}" if c.expires_at
           else f", for {c.ttl_s // 60} min once granted" if c.ttl_s else "")
    why = f" — {c.reason}" if c.reason else ""
    return f"{c.label()} [{c.owner}, since {since} UTC{ttl}, id {c.id}]{why}"


class ResourceTools:
    """The tools bound to the session this server runs for."""

    def __init__(self, store_fn=None, owner_fn=None):
        from claude_hooks.resources import api
        self._store_fn = store_fn or api.store
        self._owner_fn = owner_fn or api.owner_here

    def call(self, name: str, args: Optional[dict]) -> str:
        args = args or {}
        try:
            return self._call(name, args)
        except SpecError as e:
            return f"Not done: {e}"
        except Exception as e:      # an outage must read as one
            log.warning("resource tool %s failed", name, exc_info=True)
            return (f"The resource store is unreachable ({type(e).__name__}: "
                    f"{e}). Nothing was claimed or released.")

    def _call(self, name: str, a: dict) -> str:
        st = self._store_fn()
        if name == "resource-status":
            outs, _, _ = st.status()
            if not outs:
                return "Nothing held, nobody waiting."
            lines = []
            for o in outs:
                if o.granted:
                    lines.append("HELD   " + _line(o.claim))
                else:
                    lines.append(f"WAIT#{o.position} " + _line(o.claim))
                    if o.block:
                        lines.append(f"       waiting: {o.block.describe()}")
            return "\n".join(lines)
        if name == "resource-acquire":
            from claude_hooks.resources.cli import parse_duration
            items = [model.parse_item(s) for s in a.get("resources") or []]
            owner = self._owner_fn()
            out = st.request(items, owner, reason=str(a.get("reason") or ""),
                             ttl_s=parse_duration(a.get("ttl")))
            if out.granted:
                verb = "ALREADY HELD" if out.reused else "GRANTED"
                return (f"{verb} {out.claim.id}: {out.claim.label()}. "
                        f"Release with resource-release when done.")
            why = out.block.describe() if out.block else "busy"
            if a.get("queue"):
                note = ("\nNOTE: blocked by a claim of this same session — "
                        "release that one first." if out.self_blocked else "")
                return (f"QUEUED {out.claim.id} at position {out.position}: "
                        f"{why}.{note}\nRun this as a background command; it "
                        f"exits as soon as the claim is granted:\n"
                        f"  claude-hooks-res wait --claim {out.claim.id}")
            st.release([out.claim.id], session_id=owner.session_id,
                       event="cancelled")
            return (f"BUSY: {why}.\nTo book it and be woken when it is free, "
                    f"call again with queue=true.")
        if name == "resource-release":
            owner = self._owner_fn()
            ids = list(a.get("claims") or [])
            names = [model.parse_item(s).resource
                     for s in a.get("resources") or []]
            if not ids and not names and not a.get("all"):
                return "Name claims, resources, or all=true."
            if ids:
                done, refused = st.release(ids, session_id=owner.session_id)
            else:
                done, refused = st.release(session_id=owner.session_id,
                                           resources=names)
            lines = [f"RELEASED {c.id}: {c.label()}" for c in done]
            lines += [f"REFUSED {c.id}: held by {c.owner}, not this session"
                      for c in refused]
            return "\n".join(lines) or "Nothing of this session to release."
        if name == "resource-check":
            items = tuple(model.parse_item(s) for s in a.get("resources") or [])
            outs, capacity, _ = st.status()
            probe = Claim(id="?", seq=1 << 62, items=items)
            b = model.why_waiting(probe, [o.claim for o in outs if o.granted],
                                  [o.claim for o in outs if not o.granted],
                                  capacity)
            return (f"FREE: {probe.label()}" if b is None
                    else f"BUSY: {b.describe()}")
        if name == "resource-define":
            cap = a.get("capacity")
            if cap is not None and int(cap) == 0:
                ok = st.undefine(a["name"])
                return "Removed." if ok else "No such definition."
            st.define(a["name"], capacity=int(cap) if cap else None,
                      description=str(a.get("description") or ""))
            return f"Defined {model.normalize(a['name'])}."
        if name == "resource-events":
            rows = st.events(int(a.get("limit") or 30))
            return "\n".join(
                f"{e['at'][:19].replace('T', ' ')} {e['event']} {e['items']} "
                f"{e['alias']}@{e['host']} {e['detail']}"
                for e in reversed(rows)) or "No events."
        return f"Unknown tool {name}."


def resources_enabled(config: Optional[dict] = None) -> bool:
    """On unless ``hooks.resources.enabled`` is false."""
    if config is None:
        try:
            from claude_hooks.config import load_config
            config = load_config()
        except Exception:
            return True
    section = ((config or {}).get("hooks") or {}).get("resources") or {}
    return bool(section.get("enabled", True))
