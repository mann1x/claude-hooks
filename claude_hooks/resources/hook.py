"""What the hooks do with resource claims — so no session has to remember.

- **SessionStart**: one line saying how to claim, plus what is held and
  queued right now (:func:`session_block`).
- **UserPromptSubmit / Stop**: renew this host's claims (throttled), which
  keeps their leases fresh for the other hosts and drops claims whose
  holder has gone (:func:`touch`).
- **SessionEnd**: release the session's claims, except those a ``run``
  wrapper still holds for a job that outlives the session
  (:func:`session_end`).
- **PreToolUse (Bash)**: when a command reaches a host or GPU that another
  session holds exclusively or keeps quiet, say so once per claim — who,
  why, since when, and the command to wait for it (:func:`pre_tool_note`).
  Advisory: it never blocks the command.

Every function soft-fails: a resource store that cannot be reached must
never cost a session its prompt or its tool call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from claude_hooks.resources import model

log = logging.getLogger("claude_hooks.resources.hook")

RENEW_EVERY_S = 60.0
#: ssh options that take a separate argument.
_SSH_ARG_OPTS = set("bcDEeFIiJLlmOoPpQRSWw")
_CUDA = re.compile(r"\bCUDA_VISIBLE_DEVICES=([0-9][0-9,]*)")
_HOSTPATH = re.compile(r"^(?:[\w.-]+@)?([A-Za-z][\w.-]*):")


def enabled(config: Optional[dict]) -> bool:
    from claude_hooks.resources.tools import resources_enabled
    return resources_enabled(config)


def _state_dir() -> Path:
    root = os.environ.get("CLAUDE_HOOKS_RESOURCES_STATE_DIR")
    p = Path(root) if root else Path.home() / ".claude" / "claude-hooks-resources"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _store():
    from claude_hooks.resources import api
    return api.store()


def _short(c) -> str:
    since = (c.granted_at or c.created_at)[11:16]
    why = f" ({c.reason})" if c.reason else ""
    return f"{c.label()} — {c.owner} since {since} UTC{why}"


# ─── SessionStart ───────────────────────────────────────────────────────

HOWTO = ("Resources: before using a shared host, GPU, pod or the cloud "
         "account for real work, claim it — `resource-acquire` (MCP) or "
         "`claude-hooks-res acquire|wait|run` (shared / exclusive / quiet); "
         "a busy claim can be booked and waited for in the background. No "
         "mail needed.")


def session_block(*, event: dict, config: Optional[dict] = None) -> str:
    if not enabled(config):
        return ""
    sid = event.get("session_id") or ""
    try:
        outs, _, _ = _store().status()
    except Exception as e:
        log.debug("resources: status failed", exc_info=True)
        return f"{HOWTO} (store unreachable: {type(e).__name__})"
    if not outs:
        return HOWTO
    lines = [HOWTO]
    mine = [o for o in outs if o.claim.session_id == sid and sid]
    held = [o for o in outs if o.granted and o not in mine]
    queued = [o for o in outs if not o.granted and o not in mine]
    for o in mine:
        state = "held" if o.granted else f"queued #{o.position}"
        lines.append(f"- yours ({state}): {_short(o.claim)} [id {o.claim.id}]")
    for o in held:
        lines.append(f"- held: {_short(o.claim)}")
    for o in queued:
        lines.append(f"- queued #{o.position}: {_short(o.claim)}")
    return "\n".join(lines)


# ─── renew ──────────────────────────────────────────────────────────────

def touch(*, event: dict, config: Optional[dict] = None,
          now: Optional[float] = None) -> int:
    """Renew this host's claims, at most every :data:`RENEW_EVERY_S` per
    host. The settle inside drops claims whose holder has exited."""
    if not enabled(config):
        return 0
    now = time.time() if now is None else now
    stamp = _state_dir() / "renewed"
    try:
        if now - stamp.stat().st_mtime < RENEW_EVERY_S:
            return 0
    except OSError:
        pass
    try:
        stamp.touch()
        os.utime(stamp, (now, now))
        return _store().renew()
    except Exception:
        log.debug("resources: renew failed", exc_info=True)
        return 0


# ─── SessionEnd ─────────────────────────────────────────────────────────

def _is_claude_pid(pid: Optional[int]) -> bool:
    """Is ``pid`` a Claude Code process (a claim that follows the session)
    rather than a ``run`` wrapper (a claim that follows its job)?"""
    if not pid:
        return True
    try:
        from claude_hooks.mailbox.identity import _is_claude
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
            b"\0", b" ").decode(errors="replace")
        return _is_claude(comm, cmd)
    except OSError:
        return True        # gone, or not Linux: the session's to release


def session_end(*, event: dict, config: Optional[dict] = None) -> int:
    if not enabled(config):
        return 0
    sid = event.get("session_id") or ""
    if not sid:
        return 0
    try:
        st = _store()
        outs, _, _ = st.status()
        ids = [o.claim.id for o in outs if o.claim.session_id == sid
               and o.claim.host == st.host and _is_claude_pid(o.claim.pid)]
        if not ids:
            return 0
        done, _ = st.release(ids, session_id=sid, event="session_ended")
        return len(done)
    except Exception:
        log.debug("resources: session-end release failed", exc_info=True)
        return 0


# ─── PreToolUse ─────────────────────────────────────────────────────────

def referenced(command: str, local_host: str) -> set[str]:
    """Resources a shell command names: every ssh / scp / rsync host, and
    ``CUDA_VISIBLE_DEVICES`` GPUs (``<local host>/gpuN``). The local host
    itself is implicit (:func:`_touches`)."""
    out: set[str] = set()
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:
        toks = command.split()
    i = 0
    while i < len(toks):
        base = os.path.basename(toks[i])
        if base == "ssh":
            j = i + 1
            while j < len(toks) and toks[j].startswith("-"):
                opt = toks[j]
                if len(opt) == 2 and opt[1] in _SSH_ARG_OPTS:
                    j += 1
                j += 1
            if j < len(toks):
                host = toks[j].split("@")[-1]
                if re.match(r"^[A-Za-z][\w.-]*$", host):
                    out.add(host.lower())
            i = j + 1
            continue
        if base in ("scp", "rsync"):
            for a in toks[i + 1:]:
                m = _HOSTPATH.match(a)
                if m:
                    out.add(m.group(1).lower())
        i += 1
    for m in _CUDA.finditer(command):
        for g in m.group(1).split(","):
            if g:
                out.add(f"{local_host}/gpu{g}")
    clean = set()
    for r in out:
        try:
            clean.add(model.normalize(r))
        except model.SpecError:
            pass
    return clean


def _touches(claim, named: Iterable[str], local_host: str) -> bool:
    """Does another session's claim stand in the way of this command?

    - A **quiet** claim: any command on its host — the local one included,
      since every command runs here.
    - An **exclusive** claim: a resource the command names, one under it,
      or one above it. The local host counts only when the claim is on the
      host itself — one session holding ``solidpc/gpu0`` must not flag
      every command another session runs on solidpc.
    """
    named = set(named)
    for it in claim.items:
        if it.mode == model.QUIET:
            roots = {model.root(n) for n in named} | {local_host}
            if model.root(it.resource) in roots:
                return True
        elif it.mode == model.EXCLUSIVE:
            if it.resource == local_host:
                return True
            for n in named:
                if (n == it.resource or model.is_ancestor(it.resource, n)
                        or model.is_ancestor(n, it.resource)):
                    return True
    return False


def pre_tool_note(*, event: dict, config: Optional[dict] = None
                  ) -> Optional[str]:
    if not enabled(config) or event.get("tool_name") != "Bash":
        return None
    cmd = (event.get("tool_input") or {}).get("command") or ""
    if not cmd or "claude-hooks-res" in cmd:
        return None
    sid = event.get("session_id") or ""
    try:
        st = _store()
        named = referenced(cmd, st.host)
        held = st.snapshot()
    except Exception:
        log.debug("resources: pre-tool check failed", exc_info=True)
        return None
    hits = [c for c in held if c.session_id != sid
            and _touches(c, named, st.host)]
    if not hits:
        return None
    seen_path = _state_dir() / f"seen-{sid or 'nosession'}.json"
    try:
        seen = set(json.loads(seen_path.read_text()))
    except (OSError, ValueError):
        seen = set()
    new = [c for c in hits if c.id not in seen]
    if not new:
        return None
    try:
        seen_path.write_text(json.dumps(sorted(seen | {c.id for c in new})))
    except OSError:
        pass
    lines = ["Resource note: this command reaches resources another session "
             "holds:"]
    for c in new:
        kind = ("keeps the host QUIET" if any(i.mode == model.QUIET
                                              for i in c.items)
                else "holds it exclusively")
        lines.append(f"- {_short(c)}: {kind}")
    specs = " ".join(sorted({i.resource for c in new for i in c.items}))
    lines.append(f"If this is real work on them (not a read-only look), wait "
                 f"for them instead — in the background: `claude-hooks-res "
                 f"wait {specs} -r \"<why>\"`. (Said once per claim.)")
    return "\n".join(lines)


# ─── daemon ─────────────────────────────────────────────────────────────

class ResourceMaintenanceThread(threading.Thread):
    """In the hook daemon: renew this host's live claims and drop dead
    ones every :data:`~claude_hooks.resources.store.MAINTENANCE_INTERVAL_S`,
    so a quiet host's claims keep their leases for the other hosts, and
    prune old history once a day. Re-reads config every tick."""

    def __init__(self, config_loader, *, stop_event, interval_s=None):
        super().__init__(name="resource-maintenance", daemon=True)
        from claude_hooks.resources.store import MAINTENANCE_INTERVAL_S
        self._config_loader = config_loader
        self._stop_event = stop_event
        self._interval = float(interval_s or MAINTENANCE_INTERVAL_S)
        self._last_prune = 0.0

    def tick(self) -> None:
        cfg = self._config_loader()
        if not enabled(cfg):
            return
        st = _store()
        st.renew()
        if time.time() - self._last_prune > 86400:
            st.prune_events()
            self._last_prune = time.time()

    def run(self) -> None:  # pragma: no cover - thread loop
        import random
        if self._stop_event.wait(random.uniform(5, 30)):
            return
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                log.debug("resource maintenance tick failed: %s", e)
            self._stop_event.wait(self._interval * random.uniform(0.9, 1.1))
