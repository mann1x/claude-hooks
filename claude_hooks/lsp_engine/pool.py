"""Many narrow roots, one daemon, a bounded number of live engines.

The engine keys everything on a *project root*, and ``find_project_root``
stops at the nearest marker — a package's own ``package.json``. That is
the right root for a **language server**: rooting tsserver at a 6.1 GB
monorepo measured 0 references in 81.7 s, because it falls back to an
inferred project over the whole tree. Narrow rooting is not a compromise
there, it is the only thing that works.

It is the wrong key for a **daemon**. The cline checkout has 137 of those
roots. One daemon each would be 137 Python processes for one repository,
each with its own socket, its own lock file, its own sweeper thread, its
own idle timer and its own fleet of language servers — and no single
place to ask what is running or to tell it to stop. Nothing bounded it,
because nothing counted it: a root is discovered per request, and each
one looked like a reasonable single daemon in isolation.

So the two keys are separated. The daemon is keyed at the repository
boundary (``boundary_root_for``) and owns this pool; the pool holds one
narrowly-rooted :class:`Engine` per package that is actually touched.
Engines are created on demand, and the count is bounded twice over: an
LRU cap so a sweep across a monorepo cannot open fleets without limit,
and an idle reap so packages touched once do not hold servers for the
life of the daemon. Both bounds exist because the failure they prevent
is memory and process count, which degrade the whole host rather than
returning an error anyone would see.

Evicting an engine is safe by construction: the servers die, the open
files are forgotten, and the next request rebuilds from disk —
``did_change`` falls back to ``did_open`` and ``_ensure_open`` re-reads
content it has no stamp for. What it costs is a cold start, which is why
the caps are on idleness and count rather than on time.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from claude_hooks.lsp_engine.config import (
    DEFAULT_ENGINE_IDLE_S,
    DEFAULT_MAX_ENGINES,
    EngineConfig,
    LspServerSpec,
    find_project_root,
)
from claude_hooks.lsp_engine.engine import Engine

log = logging.getLogger("claude_hooks.lsp_engine.pool")

# The bounds live in :mod:`claude_hooks.lsp_engine.config` with the
# ``PoolConfig`` they populate — one definition, so a default and the
# operator-facing knob cannot drift apart. Re-exported here because
# this is where callers look for them.
__all__ = ["DEFAULT_ENGINE_IDLE_S", "DEFAULT_MAX_ENGINES", "EnginePool"]


class EnginePool:
    """One :class:`Engine` per narrow root, bounded and reapable.

    Thread-safe: the daemon serves requests on a thread per connection,
    and the sweeper reaps from another. The lock covers the bookkeeping
    only — engine *construction* happens under it deliberately, so two
    concurrent requests for the same new root cannot start two fleets
    for it, which is the whole failure this class exists to stop.
    """

    def __init__(
        self,
        boundary: str | os.PathLike,
        servers: list[LspServerSpec],
        config: Optional[EngineConfig] = None,
        *,
        startup_timeout: float = 10.0,
        request_timeout: float = 5.0,
        max_engines: int = DEFAULT_MAX_ENGINES,
        idle_seconds: float = DEFAULT_ENGINE_IDLE_S,
        factory: Optional[Callable[[Path], Engine]] = None,
    ) -> None:
        self._boundary = Path(boundary).resolve()
        self._servers = list(servers)
        self._config = config or EngineConfig()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._max_engines = max(1, int(max_engines))
        self._idle_seconds = float(idle_seconds)
        self._factory = factory

        self._engines: dict[Path, Engine] = {}
        self._used_at: dict[Path, float] = {}
        self._lock = threading.RLock()
        self._stopped = False

    # ─── routing ─────────────────────────────────────────────────────

    @property
    def boundary(self) -> Path:
        return self._boundary

    @property
    def max_engines(self) -> int:
        return self._max_engines

    def root_for(self, path: str | os.PathLike) -> Path:
        """The narrow root that should answer for ``path``.

        Always inside the boundary. A path outside it, or one whose
        marker walk climbs past it, is answered by the boundary itself
        rather than by an engine rooted somewhere this daemon does not
        own — a daemon that silently started a server over an unrelated
        tree would be a worse bug than a slightly wide root.
        """
        try:
            p = Path(path).resolve()
        except OSError:
            return self._boundary
        if not self._within(p):
            return self._boundary
        narrow = find_project_root(p)
        if narrow is None or not self._within(narrow):
            return self._boundary
        return narrow

    def _within(self, p: Path) -> bool:
        if p == self._boundary:
            return True
        try:
            return self._boundary in p.parents
        except OSError:  # pragma: no cover — defensive
            return False

    # ─── engines ─────────────────────────────────────────────────────

    def for_path(self, path: str | os.PathLike) -> Engine:
        """The engine for ``path``, started if this is its first use."""
        return self.for_root(self.root_for(path))

    def for_root(self, root: str | os.PathLike) -> Engine:
        key = Path(root).resolve()
        with self._lock:
            if self._stopped:
                raise RuntimeError("engine pool is stopped")
            engine = self._engines.get(key)
            if engine is None:
                self._evict_to(self._max_engines - 1, protect=key)
                engine = self._build(key)
                self._engines[key] = engine
                log.info("lsp-engine: started engine for %s (%d live)",
                         key, len(self._engines))
            self._used_at[key] = time.monotonic()
            return engine

    def existing_for_path(self, path: str | os.PathLike) -> Optional[Engine]:
        """The engine for ``path`` **only if it is already running**.

        For the paths where starting a fleet would be the wrong answer
        to the question — closing a file nobody opened, or reporting
        status — a miss is information, not a reason to build.
        """
        with self._lock:
            key = self.root_for(path)
            engine = self._engines.get(key)
            if engine is not None:
                self._used_at[key] = time.monotonic()
            return engine

    def _build(self, root: Path) -> Engine:
        if self._factory is not None:
            return self._factory(root)
        return Engine(
            project_root=root,
            servers=self._servers,
            config=self._config,
            startup_timeout=self._startup_timeout,
            request_timeout=self._request_timeout,
        )

    # ─── bounds ──────────────────────────────────────────────────────

    def _evict_to(self, limit: int, *, protect: Optional[Path] = None) -> None:
        """Shut down least-recently-used engines until ``limit`` remain.

        Called with the lock held. ``protect`` is the root about to be
        built: evicting it in the same breath as creating it would be a
        loop, and with ``max_engines=1`` that is exactly what an
        unprotected LRU would do.
        """
        while len(self._engines) > max(0, limit):
            candidates = [r for r in self._engines if r != protect]
            if not candidates:
                return
            oldest = min(candidates, key=lambda r: self._used_at.get(r, 0.0))
            log.info("lsp-engine: evicting idle engine for %s (LRU)", oldest)
            self._shutdown_root(oldest)

    def reap_idle(self, *, now: Optional[float] = None) -> list[Path]:
        """Shut down engines unused for longer than the idle window.

        Returns the roots reaped, so the caller can log or report them.
        Never reaps the last engine below the cap on time alone if it is
        in use; idleness is measured from the last *request*, so a busy
        engine never qualifies.
        """
        if self._idle_seconds <= 0:
            return []
        clock = time.monotonic() if now is None else now
        reaped: list[Path] = []
        with self._lock:
            stale = [r for r, t in self._used_at.items()
                     if r in self._engines and clock - t > self._idle_seconds]
            for root in stale:
                log.info("lsp-engine: reaping engine for %s (idle %.0fs)",
                         root, clock - self._used_at.get(root, clock))
                self._shutdown_root(root)
                reaped.append(root)
        return reaped

    # ─── lifecycle ───────────────────────────────────────────────────

    def _shutdown_root(self, root: Path) -> bool:
        """Called with the lock held."""
        engine = self._engines.pop(root, None)
        self._used_at.pop(root, None)
        if engine is None:
            return False
        try:
            engine.shutdown()
        except Exception:  # pragma: no cover — defensive
            log.exception("error shutting down engine for %s", root)
        return True

    def shutdown_root(self, root: str | os.PathLike) -> bool:
        """Stop one engine. The lifecycle op behind ``restart``."""
        with self._lock:
            return self._shutdown_root(Path(root).resolve())

    def shutdown(self) -> None:
        with self._lock:
            self._stopped = True
            for root in list(self._engines):
                self._shutdown_root(root)

    def restart_all(self) -> list[Path]:
        """Stop every engine, keeping the pool usable.

        This is what a ``reload`` actually has to do: a language server
        holds its configuration and its parsed program from the moment
        it started, so nothing short of replacing the process applies a
        changed ``cclsp.json`` or a newer server binary. Engines rebuild
        on the next request, so the caller does not have to.
        """
        with self._lock:
            roots = list(self._engines)
            for root in roots:
                self._shutdown_root(root)
            return roots

    def reconfigure(self, servers: list[LspServerSpec],
                    config: Optional[EngineConfig] = None) -> list[Path]:
        """Adopt a new server list, stopping everything built from the old.

        The config is read once, at startup, and a language server is
        configured once, when it is spawned. So an edited ``cclsp.json``
        reaches neither without replacing both — which is why editing it
        used to require killing the daemon, and on Windows there was no
        supported way to do that.
        """
        with self._lock:
            self._servers = list(servers)
            if config is not None:
                self._config = config
            return self.restart_all()

    def support_report(self) -> list[dict]:
        """What the configured servers claim, and what live ones can do.

        Falls back to a throwaway engine when nothing is running, so
        ``status`` still answers the config half of the question. An
        ``Engine`` starts no processes until a file routes to it, so
        this costs nothing.
        """
        live = self.items()
        if not live:
            return Engine(project_root=self._boundary,
                          servers=self._servers,
                          config=self._config).support_report()
        out: list[dict] = []
        for root, engine in live:
            for entry in engine.support_report():
                out.append({**entry, "root": str(root)})
        return out

    # ─── introspection ───────────────────────────────────────────────

    def live_roots(self) -> list[Path]:
        with self._lock:
            return list(self._engines)

    def live_engines(self) -> list[Engine]:
        with self._lock:
            return list(self._engines.values())

    def items(self) -> list[tuple[Path, Engine]]:
        with self._lock:
            return list(self._engines.items())

    def stats(self) -> list[dict]:
        """Per-engine state for ``status``.

        The point of one daemon per repo is that this is answerable at
        all: with a daemon per narrow root there was no process that
        could say what the repository as a whole was running.
        """
        now = time.monotonic()
        out = []
        for root, engine in self.items():
            try:
                servers = [spec.command[0] for spec in engine.active_servers()]
                open_files = engine.open_files()
            except Exception:  # pragma: no cover — defensive
                servers, open_files = [], []
            out.append({
                "root": str(root),
                "servers": servers,
                "open_files": len(open_files),
                "idle_s": round(now - self._used_at.get(root, now), 1),
            })
        out.sort(key=lambda d: d["root"])
        return out
