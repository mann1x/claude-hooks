"""Host-level supervision for the per-repository lsp_engine daemons.

The lsp_engine daemons are lazy-spawned by whoever needs one first — a
SessionStart hook, a PostToolUse edit, an MCP tool call — and they
outlive the process that started them on purpose, because the whole
value of a warm language server is that the next session does not pay
for it again. Nothing owned them afterwards. That produced three
distinct problems, all with the same root:

* **Nothing could list them.** ``status --project X`` answers for one
  project, if you already know which. There was no answer to "what is
  running on this host", which is the first question anyone asks when
  the engine misbehaves.
* **Nothing reaped them.** A daemon whose project directory has been
  deleted — a scratch checkout, a test fixture under ``/tmp`` — keeps
  its fleet alive indefinitely. Observed on this host: ten daemons
  still holding language servers for tmpdirs that no longer existed.
* **Nothing could update them.** A daemon holds the code it imported
  and the config it parsed at startup, so applying either meant killing
  it, and on Windows there was no supported way to do that. The
  ``reload`` op fixed the per-daemon half; this fixes the fleet half.

So the claude-hooks daemon — which already supervises the llamafile
embedder (:class:`~claude_hooks.embedding_manager.EmbeddingManager`) and
the chat models (:class:`~claude_hooks.chat_model_manager.ChatModelManager`)
— supervises these too.

It differs from its two siblings in one way that matters: **it does not
spawn.** An LSP daemon is spawned by the thing that has a project in
hand, because only that caller knows the root, and making the supervisor
spawn would mean guessing. This manager discovers what already exists,
from the same state directories the daemons themselves write, and it
reaps, reloads and stops. Discovery over a registry is deliberate: a
registry can disagree with reality, and the failure mode of that
disagreement is a daemon nobody can see.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.lsp_engine_manager")

#: How often the reaper wakes. LSP daemons are not latency-sensitive to
#: reap, and each pass touches every state directory on the host.
REAPER_INTERVAL_S = 300.0

#: A daemon with no attached session for this long is stopped. Well
#: above the gap between two sessions on the same project — a warm
#: engine is the point — and well below "forever", which is what it was.
DEFAULT_IDLE_SECONDS = 4 * 3600.0


class LspEngineManager:
    """Discovers, reports on, reloads and reaps lsp_engine daemons.

    Thread-safe. Every method is best-effort and soft-fails: this runs
    inside the claude-hooks daemon, and a language-server supervisor
    that can take the hook daemon down with it would be a bad trade for
    what it supervises.
    """

    def __init__(
        self,
        *,
        state_base: Optional[Path] = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        reap_orphans: bool = True,
        enabled: bool = True,
    ) -> None:
        self._state_base = (Path(state_base) if state_base
                            else Path.home() / ".claude" / "lsp-engine")
        self._idle_seconds = float(idle_seconds)
        self._reap_orphans = bool(reap_orphans)
        self._enabled = bool(enabled)
        #: project root -> monotonic time it last had an attached
        #: session. Held in memory because it is about *this* run of the
        #: supervisor: a fresh claude-hooks daemon gives every LSP
        #: daemon a fresh grace period rather than reaping a fleet it
        #: has never observed.
        self._last_busy: dict[str, float] = {}
        self._lock = threading.RLock()
        self._stop_reaper = threading.Event()
        self._reaper_thread: Optional[threading.Thread] = None

    # ─── discovery ───────────────────────────────────────────────────

    def _state_dirs(self) -> list[Path]:
        try:
            return sorted(p for p in self._state_base.iterdir() if p.is_dir())
        except OSError:
            return []

    def _project_of(self, state_dir: Path) -> Optional[Path]:
        """The root a state dir belongs to, from the hint the daemon wrote."""
        try:
            raw = (state_dir / "project").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return Path(raw) if raw else None

    def list(self) -> dict:
        """Every lsp_engine daemon this host knows about.

        The answer to "what is running", which nothing could give before.
        A dead entry is reported rather than hidden: a state directory
        with no live daemon is exactly what ``reap`` cleans, and seeing
        it is how anyone knows to.
        """
        if not self._enabled:
            return {"available": False, "reason": "lsp engine manager disabled"}
        from claude_hooks.lsp_engine.daemon import pid_is_alive
        out = []
        for state_dir in self._state_dirs():
            root = self._project_of(state_dir)
            if root is None:
                continue
            entry: dict = {
                "project": str(root),
                "state_dir": str(state_dir),
                "project_exists": self._exists(root),
                "running": False,
                "pid": None,
                "sessions": [],
                "engines": [],
                "active_servers": [],
            }
            status = self._status(root)
            if status is not None:
                entry.update({
                    "running": True,
                    "pid": status.get("pid"),
                    "sessions": status.get("sessions") or [],
                    "engines": status.get("engines") or [],
                    "active_servers": status.get("active_servers") or [],
                })
            else:
                pid = self._lock_pid(root)
                if pid is not None and pid_is_alive(pid):
                    # Socket down, process up: a daemon that is wedged
                    # rather than gone. Naming that is the difference
                    # between "reap it" and "why is nothing answering".
                    entry["pid"] = pid
                    entry["wedged"] = True
            out.append(entry)
        return {"available": True, "daemons": out,
                "idle_seconds": self._idle_seconds}

    @staticmethod
    def _exists(root: Path) -> bool:
        try:
            return root.is_dir()
        except OSError:  # pragma: no cover — defensive
            return False

    def _lock_pid(self, root: Path) -> Optional[int]:
        try:
            from claude_hooks.lsp_engine.client import daemon_pid
            return daemon_pid(root, state_base=self._state_base)
        except Exception:
            return None

    # ─── talking to one daemon ───────────────────────────────────────

    def _client(self, root: Path, session: str):
        """A connected client, or None when no daemon is listening.

        Never spawns. A supervisor that started what it was asked to
        inspect would report a fleet into existence.
        """
        from claude_hooks.lsp_engine.client import LspEngineClient
        from claude_hooks.lsp_engine.daemon import socket_path_for
        from claude_hooks.lsp_engine.ipc import _is_socket_alive
        sock = socket_path_for(root, base=self._state_base)
        if not _is_socket_alive(sock):
            return None
        client = LspEngineClient(sock, session_id=session)
        client.connect()
        return client

    def _status(self, root: Path) -> Optional[dict]:
        client = self._client(root, "claude-hooks-daemon-status")
        if client is None:
            return None
        try:
            return client.status()
        except Exception:
            return None
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover — defensive
                pass

    # ─── lifecycle ───────────────────────────────────────────────────

    def _roots(self, project: Optional[str | os.PathLike]) -> list[Path]:
        if project is not None:
            from claude_hooks.lsp_engine.daemon import daemon_root_for
            return [daemon_root_for(project)]
        roots = []
        for state_dir in self._state_dirs():
            root = self._project_of(state_dir)
            if root is not None:
                roots.append(root)
        return roots

    def reload(self, project: Optional[str | os.PathLike] = None,
               *, config: bool = True) -> dict:
        """Reload one daemon, or every daemon on the host.

        Fleet-wide is the case that did not exist before: upgrading
        claude-hooks, or editing a shared ``~/.config/cclsp/cclsp.json``,
        changes what every daemon should be running, and there was no
        way to say so short of finding and killing each one.
        """
        if not self._enabled:
            return {"available": False, "reason": "lsp engine manager disabled"}
        results = []
        for root in self._roots(project):
            client = self._client(root, "claude-hooks-daemon-reload")
            if client is None:
                results.append({"project": str(root), "reloaded": False,
                                "reason": "not running"})
                continue
            try:
                res = client.reload(config=config)
                results.append({"project": str(root), "reloaded": True,
                                "stopped": res.get("stopped") or [],
                                "reloaded_config": res.get("reloaded_config")})
            except Exception as e:
                # An older daemon has no reload op. Say which, because
                # the remedy differs: that one has to be stopped.
                results.append({"project": str(root), "reloaded": False,
                                "reason": f"{type(e).__name__}: {e}"})
            finally:
                try:
                    client.close()
                except Exception:  # pragma: no cover — defensive
                    pass
        return {"available": True, "results": results}

    def stop(self, project: Optional[str | os.PathLike] = None) -> dict:
        """Ask one or every daemon to shut down gracefully."""
        if not self._enabled:
            return {"available": False, "reason": "lsp engine manager disabled"}
        results = []
        for root in self._roots(project):
            results.append({"project": str(root),
                            "stopped": self._stop_one(root)})
        return {"available": True, "results": results}

    def _stop_one(self, root: Path) -> bool:
        client = self._client(root, "claude-hooks-daemon-stop")
        if client is None:
            return False
        try:
            client.shutdown_daemon()
            return True
        except Exception:
            log.debug("lsp daemon at %s did not ack shutdown", root,
                      exc_info=True)
            return False
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover — defensive
                pass

    # ─── reaping ─────────────────────────────────────────────────────

    def reap(self, *, now: Optional[float] = None) -> dict:
        """Stop daemons nobody is using, and clear state nothing owns.

        Two separate cases, deliberately not merged:

        * **Orphaned** — the project directory is gone. A scratch
          checkout or a test fixture under ``/tmp``; the daemon can
          never be useful again and is holding a fleet. Stopped
          immediately, with no idle grace, because there is nothing to
          be warm *for*.
        * **Idle** — the project is still there but no session has been
          attached for ``idle_seconds``. Stopped, and re-spawned lazily
          by whoever next needs it.

        A daemon with an attached session is never reaped, however long
        it has been quiet: someone is in it.
        """
        if not self._enabled:
            return {"available": False, "reason": "lsp engine manager disabled"}
        clock = time.monotonic() if now is None else now
        stopped_orphan: list[str] = []
        stopped_idle: list[str] = []
        cleaned: list[str] = []
        listing = self.list()
        for entry in listing.get("daemons", []):
            root = Path(entry["project"])
            key = str(root)
            if not entry["running"]:
                # Nothing listening. Clear the state directory so the
                # host stops accumulating them — but only when the
                # daemon is really gone, never when it is merely wedged,
                # since removing the lock of a live process invites a
                # second daemon for the same project.
                if not entry.get("wedged") and self._clean_state(entry):
                    cleaned.append(key)
                self._last_busy.pop(key, None)
                continue
            if self._reap_orphans and not entry["project_exists"]:
                if self._stop_one(root):
                    stopped_orphan.append(key)
                    self._last_busy.pop(key, None)
                continue
            if entry["sessions"]:
                with self._lock:
                    self._last_busy[key] = clock
                continue
            with self._lock:
                since = self._last_busy.setdefault(key, clock)
            if self._idle_seconds > 0 and clock - since > self._idle_seconds:
                if self._stop_one(root):
                    stopped_idle.append(key)
                    with self._lock:
                        self._last_busy.pop(key, None)
        if stopped_orphan or stopped_idle or cleaned:
            log.info("lsp reaper: %d orphaned, %d idle, %d state dirs cleaned",
                     len(stopped_orphan), len(stopped_idle), len(cleaned))
        return {"available": True, "stopped_orphaned": stopped_orphan,
                "stopped_idle": stopped_idle, "cleaned": cleaned}

    def _clean_state(self, entry: dict) -> bool:
        """Remove a state dir whose daemon is gone.

        Only for a project that no longer exists. A live project's state
        dir is cheap and its absence costs a re-resolution, so the bar
        for deleting it is higher than "nothing is listening right now".
        """
        if entry.get("project_exists"):
            return False
        try:
            shutil.rmtree(entry["state_dir"])
            return True
        except OSError:
            return False

    # ─── background thread ───────────────────────────────────────────

    def start_reaper(self) -> None:
        """Spawn the reaper thread. Called by the daemon at startup."""
        if not self._enabled:
            return
        if self._reaper_thread is not None and self._reaper_thread.is_alive():
            return
        self._stop_reaper.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="lsp-engine-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def _reaper_loop(self) -> None:
        while not self._stop_reaper.wait(timeout=REAPER_INTERVAL_S):
            try:
                self.reap()
            except Exception:  # pragma: no cover — defensive
                log.exception("lsp engine reaper pass failed")

    def shutdown(self) -> None:
        """Stop supervising. Does **not** stop the daemons.

        They outlive this process by design — that is the whole point of
        a warm engine — and a claude-hooks daemon restart during a
        deploy must not cost every open session its language servers.
        """
        self._stop_reaper.set()
        thread, self._reaper_thread = self._reaper_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
