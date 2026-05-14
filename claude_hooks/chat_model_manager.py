"""Daemon-side lifecycle for **multiple** llamafile **chat** models.

The v1.5 counterpart to :class:`claude_hooks.embedding_manager.EmbeddingManager`.
Where the embedding manager owns one binary on one port,
:class:`ChatModelManager` owns a **dict of process handles** keyed by
registry label — consultants fans out across N models per role,
``/get-advice`` swaps mid-session, and HyDE wants something small
while a 32B sits idle next to it. The capacity cap (default 2) plus
LRU eviction keeps the host's VRAM ceiling predictable.

Reuse + divergence vs EmbeddingManager
--------------------------------------

Directly copied (with per-handle scope where applicable):

- ``_maybe_wrap_for_ape`` — APE binary needs the ``/bin/sh`` bootstrap
  on POSIX; PE on Windows runs direct.
- Windows ``creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS``
  with ``stdin=DEVNULL`` to keep the chat llamafile windowless.
- ``_wait_for_health`` polling ``/health``.
- ``_port_free`` pre-flight bind check.
- ``_terminate_locked`` SIGTERM → 10 s → SIGKILL ladder.
- Reaper thread shape (per-label idle check, 0.5 s slices for
  responsive shutdown).
- GPU fallback: spawn → health-fail → sticky-CPU retry, scoped to
  the offending **label** rather than the whole manager so a 70B
  failing on GPU doesn't force a small HyDE model to CPU.

Diverges:

- ``_build_cmd(spec)`` takes a :class:`ModelSpec` parameter instead
  of reading ``self.cfg``. Drops ``--embedding --pooling`` (those
  are embedding-server-mode flags). Always passes ``-m <gguf>``
  because the chat side never ships a composite.
- ``ensure_running(label)`` resolves the label through the registry,
  reloads on mtime change, and triggers LRU eviction if the
  ``loaded`` dict would exceed ``max_concurrent_loaded``.
- ``status()`` returns the **list** of all known labels (alive +
  registered but cold); ``status(label)`` returns one row.
- ``shutdown(label=None)`` reaps all when label is None — used by
  daemon graceful-stop.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from claude_hooks.chat_model_registry import (
    DEFAULT_REGISTRY_PATH,
    ModelSpec,
    Registry,
    RegistryError,
    UnknownLabel,
)

log = logging.getLogger("claude_hooks.chat_model_manager")


# Default VRAM headroom for chat-model GPU placement decisions.
# Smaller than the embedding budget because chat models that pass
# the explicit ``ctx_size`` knob have already been right-sized by
# the operator; we just need slack for KV cache growth.
_DEFAULT_VRAM_BUDGET_MB = 4096


# --------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------- #

@dataclass
class ChatModelsConfig:
    """Knobs the manager needs that are **not** per-model.

    Per-model knobs (port, ctx_size, mode, idle_timeout) live in the
    registry's :class:`ModelSpec`. This block holds the manager-wide
    settings: the slim-binary path, concurrency cap, and reaper
    cadence.
    """

    # Path to the llamafile slim binary (no GGUF baked in). The same
    # vendor/llamafile/v0.10.1/llamafile-0.10.1 the embedding side
    # already vendors when the user picks a custom GGUF.
    llamafile_path: str = ""

    # Hard upper bound on concurrently loaded models. LRU evicts on
    # overflow. Read from the registry envelope's
    # ``max_concurrent_loaded`` so the CLI tool can tune it without
    # touching claude-hooks.json.
    max_concurrent_loaded: int = 2

    # llama.cpp server bind. Always loopback — chat models are
    # locally-supervised and never exposed to the network.
    host: str = "127.0.0.1"

    # Lifecycle knobs.
    spawn_timeout_seconds: float = 60.0  # chat models are slower to
                                          # warm than embedding
    reaper_interval_seconds: float = 60.0
    vram_budget_mb: int = _DEFAULT_VRAM_BUDGET_MB

    # Optional extra args appended after the canonical ones.
    extra_args: list[str] = field(default_factory=list)

    # Path to the registry file. Read-only — the manager never
    # writes it; the CLI tool does.
    registry_path: Path = DEFAULT_REGISTRY_PATH


def config_from_dict(cfg: dict) -> ChatModelsConfig:
    """Parse the ``cfg["chat_models"]`` block from claude-hooks.json
    into a typed :class:`ChatModelsConfig`. Unknown keys ignored.

    Caller is expected to have already checked
    ``cfg.get("chat_models", {}).get("enabled")``.
    """
    c = cfg.get("chat_models") or {}
    return ChatModelsConfig(
        llamafile_path=str(c.get("llamafile_path") or ""),
        max_concurrent_loaded=int(c.get("max_concurrent_loaded") or 2),
        host=str(c.get("host") or "127.0.0.1"),
        spawn_timeout_seconds=float(c.get("spawn_timeout_seconds") or 60.0),
        reaper_interval_seconds=float(c.get("reaper_interval_seconds") or 60.0),
        vram_budget_mb=int(c.get("vram_budget_mb") or _DEFAULT_VRAM_BUDGET_MB),
        extra_args=list(c.get("extra_args") or []),
        registry_path=Path(c.get("registry_path") or DEFAULT_REGISTRY_PATH),
    )


# --------------------------------------------------------------------- #
# ProcessHandle
# --------------------------------------------------------------------- #

@dataclass
class ProcessHandle:
    """One running llamafile chat server.

    Carries enough state to drive idle-reap (``last_activity_at``)
    and per-label GPU fallback (``gpu_offload_failed`` — sticky for
    the daemon session, scoped to this label so a 70B's CPU fallback
    doesn't punish a separately-running 7B).
    """

    label: str
    proc: subprocess.Popen
    port: int
    mode: str  # "auto" | "cpu" — the effective mode the spawn used
    last_activity_at: float
    gpu_offload_failed: bool = False


# --------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------- #

class ChatModelManager:
    """Owns ``dict[label, ProcessHandle]``. Thread-safe — all
    mutation goes through ``self.lock``.

    Public surface (RPC-facing):

    - :meth:`ensure_running(label)` — spawn (or adopt) the named
      model; evict LRU if cap exceeded; return a status dict.
    - :meth:`status(label=None)` — snapshot of one label or all known
      labels (alive + registered).
    - :meth:`shutdown(label=None)` — reap one or all. Used by daemon
      graceful-stop (``shutdown()`` with no arg) and by the CLI tool
      (``shutdown("foo")`` after a registry remove).
    - :meth:`start_reaper()` / :meth:`stop_reaper()` — reaper thread
      control. The daemon starts it once at boot.
    """

    def __init__(self, cfg: ChatModelsConfig,
                 registry: Optional[Registry] = None):
        self.cfg = cfg
        self.registry = registry or Registry(cfg.registry_path)
        self.lock = threading.Lock()
        self.handles: dict[str, ProcessHandle] = {}
        self._registry_mtime: float = self.registry.mtime()
        # Per-label sticky GPU failure cache. Keyed by label so a
        # reaped-and-restarted handle remembers its failure mode.
        self._gpu_failed: set[str] = set()
        self._stop_reaper = threading.Event()
        self._reaper_thread: Optional[threading.Thread] = None

    # ----- registry plumbing ----------------------------------- #

    def _reload_registry_if_changed(self) -> None:
        """If the registry file's mtime advanced since we last
        looked, drop our cached snapshot so the next ``get`` re-reads.

        Cheap (one ``stat`` call); called at the top of every
        ``ensure_running`` so a ``claude-hooks-models add`` followed
        by an immediate ensure picks up the new entry without a
        daemon restart."""
        current = self.registry.mtime()
        if current != self._registry_mtime:
            log.debug("registry mtime changed: %.6f -> %.6f",
                      self._registry_mtime, current)
            self._registry_mtime = current

    def _effective_max_loaded(self) -> int:
        """Read concurrency cap from the registry envelope so the
        CLI can change it live without a daemon restart."""
        try:
            env = self.registry.envelope_settings()
            return int(env["max_concurrent_loaded"])
        except (RegistryError, KeyError, ValueError):
            return self.cfg.max_concurrent_loaded

    # ----- introspection -------------------------------------- #

    def alive(self, label: str) -> bool:
        """Cheap is-it-up check. Clears the handle as a side effect
        if the process exited."""
        h = self.handles.get(label)
        if h is None:
            return False
        if h.proc.poll() is not None:
            log.info("chat llamafile %s exited (rc=%s); clearing",
                     label, h.proc.returncode)
            with self.lock:
                if label in self.handles and self.handles[label].proc.poll() is not None:
                    del self.handles[label]
            return False
        return True

    def _alive_locked(self, label: str) -> bool:
        h = self.handles.get(label)
        if h is None:
            return False
        if h.proc.poll() is not None:
            log.info("chat llamafile %s exited (rc=%s); clearing",
                     label, h.proc.returncode)
            del self.handles[label]
            return False
        return True

    def status(self, label: Optional[str] = None) -> dict:
        """RPC snapshot.

        With ``label=None``: returns ``{"models": [<row>, ...]}``
        with one row per **registered** label (alive or cold) so
        a CLI ``list`` can show full coverage.

        With ``label="foo"``: returns one row.
        """
        with self.lock:
            if label is not None:
                return {"models": [self._row_for(label)]}
            registered = self.registry.list_labels()
            rows = [self._row_for(lbl) for lbl in registered]
            # Also include any alive handles whose labels are
            # no longer in the registry (orphan after a remove);
            # `gc` will reap these.
            for lbl in self.handles:
                if lbl not in registered:
                    rows.append(self._row_for(lbl, orphan=True))
            return {
                "models": rows,
                "max_concurrent_loaded": self._effective_max_loaded(),
                "loaded": sum(1 for h in self.handles.values()
                              if h.proc.poll() is None),
            }

    def _row_for(self, label: str, *, orphan: bool = False) -> dict:
        h = self.handles.get(label)
        alive = h is not None and h.proc.poll() is None
        row = {
            "label": label,
            "alive": alive,
            "pid": h.proc.pid if alive and h is not None else None,
            "port": h.port if alive and h is not None else None,
            "mode": h.mode if alive and h is not None else None,
            "last_activity_at": h.last_activity_at if h else None,
            "idle_seconds": (time.time() - h.last_activity_at) if h else None,
            "gpu_offload_failed": label in self._gpu_failed,
            "orphan": orphan,
        }
        return row

    # ----- spawn / health ------------------------------------- #

    def _effective_mode(self, spec: ModelSpec) -> str:
        """Return the mode we'd use on the next spawn for this label.

        ``cpu`` if the registry says so OR a previous spawn fell
        back. Otherwise ``auto``."""
        if spec.mode == "cpu" or spec.label in self._gpu_failed:
            return "cpu"
        return "auto"

    def _gpu_flags(self, spec: ModelSpec) -> list[str]:
        """Decide ``-ngl 99`` vs ``--gpu disable`` for this label."""
        if self._effective_mode(spec) == "cpu":
            return ["--gpu", "disable"]
        from claude_hooks import gpu_probe  # lazy
        fits = gpu_probe.can_fit_in_vram(
            self.cfg.vram_budget_mb, headroom_mb=512
        )
        if fits is False:
            log.info(
                "gpu_probe says %s won't fit in VRAM (budget=%d + 512 headroom); "
                "spawning CPU-only",
                spec.label, self.cfg.vram_budget_mb,
            )
            return ["--gpu", "disable"]
        return ["-ngl", "99"]

    def _build_cmd(self, spec: ModelSpec) -> list[str]:
        if not self.cfg.llamafile_path:
            raise RuntimeError(
                "chat_models.llamafile_path is not set; run `install.py` "
                "to fetch the slim binary first."
            )
        if not os.path.isfile(self.cfg.llamafile_path):
            raise RuntimeError(
                f"llamafile slim binary not found at "
                f"{self.cfg.llamafile_path!r}; the install may have been "
                "interrupted."
            )
        if not os.path.isfile(spec.gguf_path):
            raise RuntimeError(
                f"GGUF for label {spec.label!r} missing at "
                f"{spec.gguf_path!r}; was it deleted after registration? "
                "Re-add with `claude-hooks-models add`."
            )
        cmd = [
            self.cfg.llamafile_path,
            "--server",
            "--host", self.cfg.host,
            "--port", str(spec.port),
            "--ctx-size", str(spec.ctx_size),
            "-m", spec.gguf_path,
        ]
        cmd.extend(self._gpu_flags(spec))
        cmd.extend(self.cfg.extra_args)
        return cmd

    def _wait_for_health(self, port: int) -> bool:
        deadline = time.monotonic() + self.cfg.spawn_timeout_seconds
        url = f"http://{self.cfg.host}:{port}/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1.0) as r:
                    if r.status == 200:
                        return True
            except urllib.error.URLError:
                pass
            except OSError:
                pass
            time.sleep(0.3)
        return False

    def _port_free(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.cfg.host, port))
        except OSError:
            return False
        return True

    @staticmethod
    def _maybe_wrap_for_ape(cmd: list[str]) -> list[str]:
        """Prepend ``/bin/sh`` on POSIX so the APE shell-bootstrap is
        what gets exec'd. No-op on Windows."""
        if sys.platform.startswith("win"):
            return cmd
        return ["/bin/sh", *cmd]

    def _spawn_once(self, spec: ModelSpec) -> subprocess.Popen:
        """Spawn one llamafile in chat mode. Same detachment pattern
        as :meth:`EmbeddingManager._spawn_once`."""
        cmd = self._build_cmd(spec)
        log.info("spawning chat llamafile [%s]: %s",
                 spec.label, " ".join(cmd))
        wrapped = self._maybe_wrap_for_ape(cmd)
        kwargs: dict = {
            "cwd": os.path.dirname(self.cfg.llamafile_path) or None,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(wrapped, **kwargs)

    # ----- LRU eviction --------------------------------------- #

    def _evict_lru_if_needed(self, exclude: str) -> list[str]:
        """If admitting one more handle would exceed the cap, reap
        the least-recently-used handle(s) until under cap. Caller
        holds ``self.lock``.

        Returns the list of labels that were evicted (may be empty).
        ``exclude`` is the label we're about to spawn — we never
        evict it (it's not in ``self.handles`` yet anyway, but be
        defensive)."""
        cap = self._effective_max_loaded()
        live = {lbl: h for lbl, h in self.handles.items()
                if h.proc.poll() is None and lbl != exclude}
        evicted: list[str] = []
        # We'll add one more, so check >= cap.
        while len(live) >= cap and live:
            # Oldest by last_activity_at.
            oldest_label = min(
                live.keys(), key=lambda k: live[k].last_activity_at
            )
            log.info(
                "LRU evict: %s (idle %.1fs, cap %d, admitting %s)",
                oldest_label,
                time.time() - live[oldest_label].last_activity_at,
                cap, exclude,
            )
            self._terminate_label_locked(oldest_label)
            evicted.append(oldest_label)
            del live[oldest_label]
        return evicted

    # ----- ensure_running ------------------------------------- #

    def ensure_running(self, label: str) -> dict:
        """Spawn or adopt the llamafile for ``label``. Public RPC
        entry point.

        Returns ``{label, port, ready, mode, spawned, evicted: [...]}``.
        Raises :class:`UnknownLabel` if the registry has no such entry,
        or :class:`RuntimeError` on spawn failure.
        """
        self._reload_registry_if_changed()
        spec = self.registry.get(label)  # raises UnknownLabel cleanly

        with self.lock:
            if self._alive_locked(label):
                h = self.handles[label]
                h.last_activity_at = time.time()
                return {
                    "label": label,
                    "ready": True,
                    "port": h.port,
                    "mode": h.mode,
                    "spawned": False,
                    "evicted": [],
                }

            # Evict LRU to make room *before* binding the port.
            evicted = self._evict_lru_if_needed(label)

            if not self._port_free(spec.port):
                raise RuntimeError(
                    f"chat-model port {spec.port} for {label!r} is "
                    "already in use by another process. Free it and "
                    "retry."
                )

            try:
                proc = self._spawn_once(spec)
            except OSError as e:
                raise RuntimeError(
                    f"chat llamafile {label!r} spawn failed ({e}); "
                    f"check that {self.cfg.llamafile_path!r} is "
                    "executable."
                ) from e

            self.handles[label] = ProcessHandle(
                label=label,
                proc=proc,
                port=spec.port,
                mode=self._effective_mode(spec),
                last_activity_at=time.time(),
                gpu_offload_failed=(label in self._gpu_failed),
            )

            if self._wait_for_health(spec.port):
                log.info(
                    "chat llamafile %s ready on port %d (pid=%s, mode=%s)",
                    label, spec.port, proc.pid,
                    self.handles[label].mode,
                )
                return {
                    "label": label,
                    "ready": True,
                    "port": spec.port,
                    "mode": self.handles[label].mode,
                    "spawned": True,
                    "evicted": evicted,
                }

            # Health failed. If we tried GPU, fall back to CPU once.
            self._terminate_label_locked(label)
            if (self._effective_mode(spec) == "auto"
                    and label not in self._gpu_failed):
                log.warning(
                    "chat llamafile %s didn't come up on GPU within %.1fs; "
                    "falling back to CPU for this session",
                    label, self.cfg.spawn_timeout_seconds,
                )
                self._gpu_failed.add(label)
                try:
                    proc = self._spawn_once(spec)
                except OSError as e:
                    raise RuntimeError(
                        f"chat llamafile {label!r} CPU fallback "
                        f"spawn failed: {e}"
                    ) from e
                self.handles[label] = ProcessHandle(
                    label=label, proc=proc, port=spec.port, mode="cpu",
                    last_activity_at=time.time(),
                    gpu_offload_failed=True,
                )
                if self._wait_for_health(spec.port):
                    log.info(
                        "chat llamafile %s ready on port %d (pid=%s, mode=cpu)",
                        label, spec.port, proc.pid,
                    )
                    return {
                        "label": label,
                        "ready": True,
                        "port": spec.port,
                        "mode": "cpu",
                        "spawned": True,
                        "gpu_fallback": True,
                        "evicted": evicted,
                    }
                self._terminate_label_locked(label)

            raise RuntimeError(
                f"chat llamafile {label!r} did not respond on port "
                f"{spec.port} within {self.cfg.spawn_timeout_seconds}s"
            )

    def touch(self, label: str) -> None:
        """Reset the idle clock for a label without doing any work.
        Used by clients that have a long-lived connection and want
        to defer the reaper."""
        with self.lock:
            h = self.handles.get(label)
            if h is not None:
                h.last_activity_at = time.time()

    # ----- reap ----------------------------------------------- #

    def _terminate_label_locked(
        self, label: str, *, grace_seconds: float = 10.0,
    ) -> None:
        """SIGTERM, then SIGKILL after grace. Caller holds the lock.
        No-op if the label isn't loaded."""
        h = self.handles.pop(label, None)
        if h is None:
            return
        pid = h.proc.pid
        try:
            log.info("terminating chat llamafile %s pid=%s (SIGTERM)",
                     label, pid)
            h.proc.terminate()
        except OSError as e:
            log.warning("SIGTERM failed for %s: %s", label, e)
        try:
            h.proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            log.warning(
                "chat llamafile %s pid=%s did not stop after %.1fs; SIGKILL",
                label, pid, grace_seconds,
            )
            try:
                h.proc.kill()
                h.proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.error("SIGKILL failed for %s: %s", label, e)

    def maybe_reap(self) -> list[str]:
        """Called by the reaper thread. Returns the list of labels
        whose idle timeouts elapsed (and were therefore reaped)."""
        reaped: list[str] = []
        with self.lock:
            now = time.time()
            for label, h in list(self.handles.items()):
                if h.proc.poll() is not None:
                    # Process exited externally; clean up the entry.
                    del self.handles[label]
                    continue
                try:
                    spec = self.registry.get(label)
                    idle_timeout = spec.idle_timeout_seconds
                except (UnknownLabel, RegistryError):
                    # Label was removed from the registry while running
                    # — treat as orphan, reap immediately.
                    log.info("reaper: %s missing from registry; reaping",
                             label)
                    self._terminate_label_locked(label)
                    reaped.append(label)
                    continue
                idle = now - h.last_activity_at
                if idle >= idle_timeout:
                    log.info(
                        "reaper: chat llamafile %s idle %.1fs > %.1fs; reaping",
                        label, idle, idle_timeout,
                    )
                    self._terminate_label_locked(label)
                    reaped.append(label)
        return reaped

    def shutdown(self, label: Optional[str] = None) -> dict:
        """Reap one or all. ``label=None`` stops the reaper thread
        and reaps everything (daemon graceful-stop)."""
        if label is None:
            self._stop_reaper.set()
            with self.lock:
                for lbl in list(self.handles.keys()):
                    self._terminate_label_locked(lbl)
            return {"stopped": True}
        with self.lock:
            existed = label in self.handles
            self._terminate_label_locked(label)
            return {"stopped": existed, "label": label}

    def gc(self) -> dict:
        """Reap any handle whose label is no longer in the registry.
        Public so the CLI tool can invoke it explicitly."""
        with self.lock:
            registered = set(self.registry.list_labels())
            evicted: list[str] = []
            for label in list(self.handles.keys()):
                if label not in registered:
                    log.info("gc: %s missing from registry; reaping", label)
                    self._terminate_label_locked(label)
                    evicted.append(label)
            return {"evicted": evicted}

    # ----- reaper thread -------------------------------------- #

    def start_reaper(self) -> None:
        if self._reaper_thread is not None and self._reaper_thread.is_alive():
            return
        self._stop_reaper.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="chat-model-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def _reaper_loop(self) -> None:
        while not self._stop_reaper.is_set():
            slept = 0.0
            while slept < self.cfg.reaper_interval_seconds:
                if self._stop_reaper.is_set():
                    return
                time.sleep(0.5)
                slept += 0.5
            try:
                self.maybe_reap()
            except Exception as e:  # pragma: no cover - defensive
                log.warning("chat-model reaper error: %s", e)
