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
            status = self._status(root, state_dir)
            if status is not None:
                entry.update({
                    "running": True,
                    "pid": status.get("pid"),
                    "sessions": status.get("sessions") or [],
                    "engines": status.get("engines") or [],
                    "active_servers": status.get("active_servers") or [],
                })
                # The daemon is the authority on what it serves; the
                # hint file is a breadcrumb that predates the move to
                # repository boundaries and can now name a package
                # inside what the daemon actually owns.
                served = status.get("project")
                if served and str(served) != str(root):
                    entry["serves"] = str(served)
                    entry["superseded"] = True
            else:
                pid = self._lock_pid(root, state_dir)
                if pid is not None and pid_is_alive(pid):
                    # Socket down, process up: a daemon that is wedged
                    # rather than gone. Naming that is the difference
                    # between "reap it" and "why is nothing answering".
                    entry["pid"] = pid
                    entry["wedged"] = True
            out.append(entry)
        try:
            stateless = self.stateless_daemons()
        except Exception:  # pragma: no cover — defensive
            stateless = []
        return {"available": True, "daemons": out,
                "stateless": stateless,
                "idle_seconds": self._idle_seconds}

    @staticmethod
    def _exists(root: Path) -> bool:
        try:
            return root.is_dir()
        except OSError:  # pragma: no cover — defensive
            return False

    def _lock_pid(self, root: Path,
                  state_dir: Optional[Path] = None) -> Optional[int]:
        """The pid in **this state dir's** lock file.

        Same trap as :meth:`_socket_for`, and it survived the first fix
        because only the socket path was corrected: ``daemon_pid()``
        recomputes ``lock_path_for(root)``, which normalises a stale
        narrow root up to its repository and returns the *boundary*
        daemon's live pid. Every stale directory under a live repo was
        then reported ``wedged`` with a pid that is not its own — and
        ``wedged`` blocks state cleanup, so those directories could
        never be cleared. Seen in ``lsp list`` output after the deploy,
        not in a test.
        """
        try:
            if state_dir is not None:
                lock = state_dir / "daemon.lock"
                if not lock.is_file():
                    return None
                first = lock.read_text(encoding="ascii").splitlines()[0]
                return int(first.strip())
            from claude_hooks.lsp_engine.client import daemon_pid
            return daemon_pid(root, state_base=self._state_base)
        except Exception:
            return None

    def stateless_daemons(self) -> list[dict]:
        """Live daemons with no state directory left.

        A daemon whose state dir was removed — by ``lsp_engine cleanup``,
        by ``restart``, or by this reaper — is invisible to every
        disk-based lookup, and on POSIX its socket inode is unlinked, so
        nothing can connect to it either. It keeps serving the
        connections it already has and can never be reached again: the
        purest form of "it cannot be updated and I have to close the
        session".

        Two such daemons existed on this host the day this was written,
        which is why discovery does not stop at the filesystem.

        They are reported and never auto-reaped. An unlinked socket does
        not mean nobody is attached — existing connections survive it —
        so stopping one needs a signal, and a signal to a daemon a live
        session is still talking to is the user's call, not a reaper's.
        Linux-only: ``/proc`` is the cheap answer and there is no
        dependency-free equivalent elsewhere.
        """
        out: list[dict] = []
        proc = Path("/proc")
        if not proc.is_dir():
            return out
        # Compare against the hints themselves, not against a
        # recomputed state dir: recomputing normalises a pre-boundary
        # daemon's narrow root up to the repository, which has its own
        # live state dir, so every such daemon looked discoverable and
        # none was reported. Found by the count coming back zero
        # against two processes visible in ``ps``.
        hinted = {str(r) for r in (
            self._project_of(d) for d in self._state_dirs()) if r is not None}
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            parts = [a.decode("utf-8", "replace") for a in argv if a]
            if not (any("claude_hooks.lsp_engine" in a for a in parts)
                    and "daemon" in parts and "--project" in parts):
                continue
            try:
                root = Path(parts[parts.index("--project") + 1])
            except (ValueError, IndexError):  # pragma: no cover — defensive
                continue
            if str(root) in hinted:
                continue          # discoverable the normal way
            out.append({
                "pid": int(entry.name),
                "project": str(root),
                "project_exists": self._exists(root),
                "stateless": True,
                "reason": "state directory was removed; unreachable over IPC",
            })
        return out

    # ─── talking to one daemon ───────────────────────────────────────

    def _socket_for(self, root: Path,
                    state_dir: Optional[Path] = None):
        """Where to knock for the daemon a state dir belongs to.

        The state directory\'s **own** socket, not one recomputed from
        the project hint. Those used to be the same thing; since the
        daemon moved to the repository boundary they are not, and
        recomputing gets both directions wrong:

        * every pre-boundary state dir under one repository recomputes
          to that repository\'s socket, so one live daemon is reported
          once per stale directory — 19 "daemons" for 4 processes,
          observed here during the migration;
        * a daemon started before the change is listening on the old
          narrow-rooted socket, which nothing recomputes to any more,
          so it becomes invisible and therefore unreapable.

        On Windows the pipe has no filesystem entry, so there is nothing
        to read beside the state dir and the name has to be derived.
        """
        from claude_hooks.lsp_engine.daemon import _is_windows, socket_path_for
        if state_dir is not None and not _is_windows():
            return state_dir / "daemon.sock"
        return socket_path_for(root, base=self._state_base)

    def _client(self, root: Path, session: str,
                state_dir: Optional[Path] = None):
        """A connected client, or None when no daemon is listening.

        Never spawns. A supervisor that started what it was asked to
        inspect would report a fleet into existence.
        """
        from claude_hooks.lsp_engine.client import LspEngineClient
        from claude_hooks.lsp_engine.ipc import _is_socket_alive
        sock = self._socket_for(root, state_dir)
        if not _is_socket_alive(sock):
            return None
        client = LspEngineClient(sock, session_id=session)
        client.connect()
        return client

    def _status(self, root: Path,
                state_dir: Optional[Path] = None) -> Optional[dict]:
        client = self._client(root, "claude-hooks-daemon-status", state_dir)
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

    def _roots(
        self, project: Optional[str | os.PathLike],
    ) -> list[tuple[Path, Optional[Path]]]:
        """(project root, its state dir) for each daemon to act on.

        The state dir travels with the root because it is what says
        *which socket*; see :meth:`_socket_for`.
        """
        if project is not None:
            from claude_hooks.lsp_engine.daemon import (
                daemon_root_for, project_dir,
            )
            root = daemon_root_for(project)
            return [(root, project_dir(root, base=self._state_base))]
        out: list[tuple[Path, Optional[Path]]] = []
        for state_dir in self._state_dirs():
            root = self._project_of(state_dir)
            if root is not None:
                out.append((root, state_dir))
        return out

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
        for root, state_dir in self._roots(project):
            client = self._client(root, "claude-hooks-daemon-reload",
                                  state_dir)
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
        for root, state_dir in self._roots(project):
            results.append({"project": str(root),
                            "stopped": self._stop_one(root, state_dir)})
        return {"available": True, "results": results}

    def _stop_one(self, root: Path,
                  state_dir: Optional[Path] = None) -> bool:
        client = self._client(root, "claude-hooks-daemon-stop", state_dir)
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
            state_dir = Path(entry["state_dir"])
            if self._reap_orphans and not entry["project_exists"]:
                if self._stop_one(root, state_dir):
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
                if self._stop_one(root, state_dir):
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
