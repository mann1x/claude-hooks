"""Daemon-side lifecycle for the llamafile embedding server.

The :class:`EmbeddingManager` is owned by ``claude-hooks-daemon``
(see :mod:`claude_hooks.daemon`) and exposed over the daemon's TCP
RPC as ``_embedding_ensure`` / ``_embedding_status`` /
``_embedding_shutdown``. Unlike the consultants forwarder, this
manager **does not proxy HTTP traffic** — clients
(:class:`claude_hooks.embedders.LlamafileEmbedder`) talk to the
llamafile directly on the configured port. The manager only spawns
the binary, polls its ``/health`` endpoint, idle-reaps after
``idle_timeout_seconds`` of no ``ensure_running()`` calls, and
respawns transparently on the next ping.

Design notes
------------

- **Stdlib only.** Imports stay at the same floor as the daemon
  (Python 3.9+). No tomli, no asyncio.
- **Same idle-reaper pattern** as
  :class:`claude_hooks.consultants_forwarder.EngineManager` so the
  shape is familiar to anyone who's read that code. Defaults are
  different (5 min vs 30 min idle) and the spawn command is the
  llamafile binary directly, not a Python module.
- **GPU mode** is taken from :mod:`claude_hooks.gpu_probe`. With
  ``mode="auto"`` (the install default) the manager passes
  ``-ngl 99`` (offload all layers) when the probe reports a GPU
  vendor and the model is expected to fit in VRAM. If spawn or the
  health probe fails, the manager reaps and respawns with
  ``--gpu disable``; the resulting CPU mode sticks for the rest of
  the daemon session so we don't crash-loop on an undersized GPU.
- **No PID file.** Single-daemon guarantee comes for free from the
  daemon's existing TCP socket lock; running two ``EmbeddingManager``
  instances against the same fixed port is impossible because two
  daemons can't bind 47018 simultaneously.

The fixed embedding port (default ``38092``) is the same number
:class:`LlamafileEmbedder` connects to by default — keep them in
sync if you change one.
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
from typing import Optional

log = logging.getLogger("claude_hooks.embedding_manager")


# Pooling mode names accepted by the llama.cpp server. We pin
# ``last`` for qwen3-embedding (the GGUF metadata reports
# pooling_type=3=LAST); the config can override.
_VALID_POOLING = {"none", "mean", "cls", "last", "rank"}


# Approximate VRAM footprint for the default qwen3-embedding-0.6B at
# 16k ctx with Q8 KV. Used by the gpu-probe pre-flight to decide
# whether ``-ngl 99`` is safe. Conservative — actual measured RSS is
# under 1.6 GB on the bench machine, but headroom helps coexist with
# whatever else the user is running on the same GPU.
_DEFAULT_VRAM_BUDGET_MB = 2048


# ---------------------------------------------------------------- #
# config
# ---------------------------------------------------------------- #

@dataclass
class EmbeddingConfig:
    """All knobs the manager needs.

    Resolved by the daemon at startup from
    ``cfg["embedding"]`` in claude-hooks.json. Defaults here mirror
    the v1.4 plan ("composite llamafile, 16k ctx, 5-min idle,
    auto-GPU").
    """

    # Path to the llamafile (composite by default — slim+GGUF baked
    # into one binary). If empty, EmbeddingManager refuses to spawn
    # with a clean error.
    llamafile_path: str = ""

    # If the user supplied a custom GGUF, the manager uses the slim
    # binary + ``-m <path>``. Set ``llamafile_path`` to the slim
    # binary and ``model_gguf`` to the GGUF.
    model_gguf: str = ""

    # llama.cpp server bind. Always 127.0.0.1 — the embedder is local
    # and we don't want to expose it to the network.
    host: str = "127.0.0.1"
    port: int = 38092

    # Effective context size; must match what the LlamafileEmbedder /
    # CompositeEmbedder primary expects. The composite shipped with
    # claude-hooks is built with --ctx-size 16384 baked in but the
    # `--ctx-size` CLI flag still takes precedence.
    ctx_size: int = 16384

    # Pooling mode; qwen3-embedding wants ``last``.
    pooling: str = "last"

    # "auto" | "cpu" — auto tries the GPU first and falls back to
    # CPU on spawn/health failure (sticky for the session). "cpu"
    # passes --gpu disable on every spawn and never probes.
    mode: str = "auto"

    # When mode=auto, override the VRAM budget check; ignored when
    # mode=cpu.
    vram_budget_mb: int = _DEFAULT_VRAM_BUDGET_MB

    # Lifecycle knobs.
    spawn_timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 300.0
    reaper_interval_seconds: float = 60.0

    # Optional extra args appended after the canonical ones.
    extra_args: list[str] = field(default_factory=list)

    # cwd for the llamafile subprocess. Defaults to its own
    # directory so any relative paths inside the composite resolve
    # next to the binary.
    cwd: str = ""


# ---------------------------------------------------------------- #
# manager
# ---------------------------------------------------------------- #

class EmbeddingManager:
    """Owns the llamafile subprocess. Thread-safe — all mutation of
    ``self.proc`` and ``self.last_activity_at`` goes through
    ``self.lock``."""

    def __init__(self, cfg: EmbeddingConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.last_activity_at: float = time.time()
        self.gpu_offload_failed: bool = False
        self._stop_reaper = threading.Event()
        self._reaper_thread: Optional[threading.Thread] = None
        # Sanity-check pooling at construct time — silently dropping
        # an invalid value at spawn time would be very confusing.
        if cfg.pooling not in _VALID_POOLING:
            raise ValueError(
                f"invalid pooling mode {cfg.pooling!r}; "
                f"expected one of {sorted(_VALID_POOLING)}"
            )
        if cfg.mode not in ("auto", "cpu"):
            raise ValueError(
                f"invalid mode {cfg.mode!r}; expected 'auto' or 'cpu'"
            )

    # ----- introspection ---------------------------------------- #

    def alive(self) -> bool:
        """Cheap, lock-free is-it-up check.

        Returns ``False`` if the process exited; clears ``self.proc``
        as a side effect when it does — that's safe because every
        call site that mutates ``self.proc`` already holds the lock
        and re-checks via :meth:`_alive_locked`.
        """
        if self.proc is None:
            return False
        if self.proc.poll() is not None:
            log.info("llamafile exited (rc=%s); clearing",
                     self.proc.returncode)
            with self.lock:
                # Re-check under the lock to avoid double-clearing.
                if self.proc is not None and self.proc.poll() is not None:
                    self.proc = None
            return False
        return True

    def _alive_locked(self) -> bool:
        if self.proc is None:
            return False
        if self.proc.poll() is not None:
            log.info("llamafile exited (rc=%s); clearing",
                     self.proc.returncode)
            self.proc = None
            return False
        return True

    def status(self) -> dict:
        """Snapshot for ``_embedding_status`` RPC."""
        with self.lock:
            alive = self._alive_locked()
            return {
                "alive": alive,
                "pid": self.proc.pid if (alive and self.proc) else None,
                "port": self.cfg.port if alive else None,
                "mode": self._effective_mode(),
                "last_activity_at": self.last_activity_at,
                "idle_seconds": time.time() - self.last_activity_at,
                "idle_timeout_seconds": self.cfg.idle_timeout_seconds,
                "gpu_offload_failed": self.gpu_offload_failed,
            }

    # ----- spawn / health --------------------------------------- #

    def _effective_mode(self) -> str:
        """Return the mode we'd use on the next spawn.

        ``cpu`` if the user asked for it OR a previous spawn fell
        back. Otherwise ``auto`` (which means "try GPU"). The
        installer-level ``cpu`` choice is sticky permanently; the
        runtime fallback is sticky for the daemon session.
        """
        if self.cfg.mode == "cpu" or self.gpu_offload_failed:
            return "cpu"
        return "auto"

    def _gpu_flags(self) -> list[str]:
        """Decide whether to pass ``-ngl 99`` or ``--gpu disable``.

        Calls :mod:`claude_hooks.gpu_probe` lazily so unit tests can
        stub it out without importing nvidia-smi machinery.
        """
        if self._effective_mode() == "cpu":
            return ["--gpu", "disable"]
        # auto mode: pre-flight VRAM check. ``can_fit_in_vram``
        # returns None when the probe can't determine VRAM (vulkan
        # vendor or no GPU at all) — treat that as "try GPU anyway"
        # because the fat binary's runtime probe will sort it out.
        from claude_hooks import gpu_probe  # lazy

        fits = gpu_probe.can_fit_in_vram(
            self.cfg.vram_budget_mb, headroom_mb=512
        )
        if fits is False:
            # We're sure the offload won't fit — skip the GPU path
            # rather than crash-loop on OOM.
            log.info(
                "gpu_probe says model won't fit in VRAM "
                "(budget=%d MB + 512 headroom); spawning CPU-only",
                self.cfg.vram_budget_mb,
            )
            return ["--gpu", "disable"]
        return ["-ngl", "99"]

    def _build_cmd(self) -> list[str]:
        if not self.cfg.llamafile_path:
            raise RuntimeError(
                "embedding.llamafile_path is not set; run `install.py` "
                "to download the composite or build a custom one."
            )
        if not os.path.isfile(self.cfg.llamafile_path):
            raise RuntimeError(
                f"llamafile not found at {self.cfg.llamafile_path!r}; "
                "the install may have been interrupted."
            )
        cmd = [
            self.cfg.llamafile_path,
            "--server",
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "--embedding",
            "--pooling", self.cfg.pooling,
            "--ctx-size", str(self.cfg.ctx_size),
        ]
        if self.cfg.model_gguf:
            # Custom GGUF path. The slim binary needs ``-m`` to find
            # the model; the composite already has it baked in.
            cmd.extend(["-m", self.cfg.model_gguf])
        cmd.extend(self._gpu_flags())
        cmd.extend(self.cfg.extra_args)
        return cmd

    def _wait_for_health(self) -> bool:
        deadline = time.monotonic() + self.cfg.spawn_timeout_seconds
        url = f"http://{self.cfg.host}:{self.cfg.port}/health"
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

    def _port_free(self) -> bool:
        """Best-effort pre-flight: can we bind the chosen port?"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.cfg.host, self.cfg.port))
        except OSError:
            return False
        return True

    def _spawn_once(self, cwd: Optional[str]) -> subprocess.Popen:
        """Run :class:`subprocess.Popen` with detached session.
        Stdout/stderr go to DEVNULL — the llama.cpp server is chatty
        and the daemon's journal would drown in it. Operators who
        need the output can re-run the binary by hand.

        APE / Cosmopolitan-libc note: llamafile binaries start with
        ``MZqFpD='`` magic that the Linux kernel doesn't recognise
        as a binfmt directly (without ``binfmt_misc`` registration
        for APE). The bytes are simultaneously a valid POSIX shell
        script whose first action is to re-exec the kernel-level
        entry point — so on POSIX we invoke the binary through
        ``/bin/sh``, which runs the shell prefix and lets the
        embedded ``exec`` jump to the actual program. On Windows the
        binary is a normal PE and Popen launches it directly.
        """
        cmd = self._build_cmd()
        log.info("spawning llamafile: %s", " ".join(cmd))
        wrapped = self._maybe_wrap_for_ape(cmd)
        return subprocess.Popen(
            wrapped,
            cwd=cwd or None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _maybe_wrap_for_ape(cmd: list[str]) -> list[str]:
        """Prepend ``/bin/sh`` on POSIX so the APE shell-bootstrap is
        what gets exec'd. No-op on Windows."""
        if sys.platform.startswith("win"):
            return cmd
        return ["/bin/sh", *cmd]

    def ensure_running(self) -> dict:
        """Public entry point — spawn (or adopt) the llamafile, return
        a status dict the RPC layer can pass to clients.

        Touches ``last_activity_at`` on every call regardless of
        whether we spawned, so the embedder's "ping-then-embed"
        pattern resets the idle clock correctly.
        """
        with self.lock:
            if self._alive_locked():
                self.last_activity_at = time.time()
                return {
                    "ready": True,
                    "port": self.cfg.port,
                    "mode": self._effective_mode(),
                    "spawned": False,
                }

            # Bind-port pre-flight: refuse if something else is
            # already listening on our port (most likely an orphan
            # from a previous crash). Surface a clean error rather
            # than silently competing for the port.
            if not self._port_free():
                raise RuntimeError(
                    f"embedding port {self.cfg.port} is already in "
                    f"use by another process. Free it (e.g. "
                    f"`fuser -k {self.cfg.port}/tcp` on Linux, "
                    f"`Stop-Process` on Windows) and retry."
                )

            cwd = self.cfg.cwd or os.path.dirname(self.cfg.llamafile_path)
            try:
                self.proc = self._spawn_once(cwd)
            except OSError as e:
                raise RuntimeError(
                    f"llamafile spawn failed ({e}); "
                    f"check that {self.cfg.llamafile_path!r} is "
                    "executable and the platform is supported."
                ) from e

            if self._wait_for_health():
                self.last_activity_at = time.time()
                log.info(
                    "llamafile ready on port %d (pid=%s, mode=%s)",
                    self.cfg.port, self.proc.pid, self._effective_mode(),
                )
                return {
                    "ready": True,
                    "port": self.cfg.port,
                    "mode": self._effective_mode(),
                    "spawned": True,
                }

            # Spawn succeeded but health never returned 200. If we
            # were trying the GPU path, fall back to CPU once. Any
            # other failure surfaces as a hard error so the
            # CompositeEmbedder can fail over to its primary.
            self._terminate_locked()
            if self._effective_mode() == "auto" and not self.gpu_offload_failed:
                log.warning(
                    "llamafile didn't come up on GPU within %.1fs; "
                    "falling back to CPU-only for the rest of the session",
                    self.cfg.spawn_timeout_seconds,
                )
                self.gpu_offload_failed = True
                try:
                    self.proc = self._spawn_once(cwd)
                except OSError as e:
                    raise RuntimeError(
                        f"llamafile CPU fallback spawn failed: {e}"
                    ) from e
                if self._wait_for_health():
                    self.last_activity_at = time.time()
                    log.info("llamafile ready on port %d (pid=%s, mode=cpu)",
                             self.cfg.port, self.proc.pid)
                    return {
                        "ready": True,
                        "port": self.cfg.port,
                        "mode": "cpu",
                        "spawned": True,
                        "gpu_fallback": True,
                    }
                self._terminate_locked()
            raise RuntimeError(
                f"llamafile did not respond on port {self.cfg.port} "
                f"within {self.cfg.spawn_timeout_seconds}s"
            )

    def touch(self) -> None:
        """Reset the idle clock without doing any work. Useful for
        external pingers (the embedder calls this implicitly via
        ``ensure_running`` but a periodic warmup could use it
        directly)."""
        self.last_activity_at = time.time()

    # ----- reap ------------------------------------------------- #

    def _terminate_locked(self, *, grace_seconds: float = 10.0) -> None:
        """SIGTERM, then SIGKILL after grace. Caller holds
        ``self.lock``. No-op if already cleared."""
        if self.proc is None:
            return
        pid = self.proc.pid
        try:
            log.info("terminating llamafile pid=%s (SIGTERM)", pid)
            self.proc.terminate()
        except OSError as e:
            log.warning("SIGTERM failed: %s", e)
        try:
            self.proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            log.warning("llamafile pid=%s did not stop after %.1fs; SIGKILL",
                        pid, grace_seconds)
            try:
                self.proc.kill()
                self.proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.error("SIGKILL failed: %s", e)
        self.proc = None

    def maybe_reap(self) -> bool:
        """Called by the reaper thread. Returns ``True`` if a reap
        actually happened."""
        with self.lock:
            if self.proc is None:
                return False
            idle = time.time() - self.last_activity_at
            if idle < self.cfg.idle_timeout_seconds:
                return False
            log.info(
                "reaper: llamafile idle %.1fs > %.1fs; reaping",
                idle, self.cfg.idle_timeout_seconds,
            )
            self._terminate_locked()
            return True

    def shutdown(self) -> None:
        """Stop the reaper thread (if running) and reap the
        llamafile. Idempotent."""
        self._stop_reaper.set()
        with self.lock:
            self._terminate_locked()

    # ----- reaper thread --------------------------------------- #

    def start_reaper(self) -> None:
        """Spawn the idle-reaper background thread. Called by the
        daemon after ``EmbeddingManager`` is wired up; safe to call
        twice (second call is a no-op)."""
        if self._reaper_thread is not None and self._reaper_thread.is_alive():
            return
        self._stop_reaper.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="embedding-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def _reaper_loop(self) -> None:
        while not self._stop_reaper.is_set():
            # Sleep in small slices so shutdown is responsive.
            slept = 0.0
            while slept < self.cfg.reaper_interval_seconds:
                if self._stop_reaper.is_set():
                    return
                time.sleep(0.5)
                slept += 0.5
            try:
                self.maybe_reap()
            except Exception as e:  # pragma: no cover - defensive
                log.warning("reaper error: %s", e)


# ---------------------------------------------------------------- #
# config-from-dict helper (used by daemon.py to build the manager)
# ---------------------------------------------------------------- #

def config_from_dict(cfg: dict) -> EmbeddingConfig:
    """Parse the ``cfg["embedding"]`` block from claude-hooks.json
    into a typed :class:`EmbeddingConfig`. Unknown keys are silently
    ignored to match the rest of claude-hooks's config style.

    Caller is expected to have already checked
    ``cfg.get("embedding", {}).get("enabled")``.
    """
    e = cfg.get("embedding") or {}
    return EmbeddingConfig(
        llamafile_path=str(e.get("llamafile_path") or ""),
        model_gguf=str(e.get("model_gguf") or ""),
        host=str(e.get("host") or "127.0.0.1"),
        port=int(e.get("port") or 38092),
        ctx_size=int(e.get("ctx_size") or 16384),
        pooling=str(e.get("pooling") or "last"),
        mode=str(e.get("mode") or "auto"),
        vram_budget_mb=int(e.get("vram_budget_mb") or _DEFAULT_VRAM_BUDGET_MB),
        spawn_timeout_seconds=float(e.get("spawn_timeout_seconds") or 30.0),
        idle_timeout_seconds=float(e.get("idle_timeout_seconds") or 300.0),
        reaper_interval_seconds=float(e.get("reaper_interval_seconds") or 60.0),
        extra_args=list(e.get("extra_args") or []),
        cwd=str(e.get("cwd") or ""),
    )


# ---------------------------------------------------------------- #
# standalone main — useful for one-off testing
# ---------------------------------------------------------------- #

def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    """Tiny CLI to spawn / status / shutdown for manual testing.
    Not used in production — the daemon wires the manager itself."""
    import argparse

    parser = argparse.ArgumentParser(prog="embedding-manager")
    parser.add_argument("action", choices=("spawn", "status", "shutdown"))
    parser.add_argument("--llamafile", required=True)
    parser.add_argument("--port", type=int, default=38092)
    parser.add_argument("--mode", choices=("auto", "cpu"), default="auto")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = EmbeddingConfig(
        llamafile_path=args.llamafile, port=args.port, mode=args.mode,
    )
    mgr = EmbeddingManager(cfg)
    if args.action == "spawn":
        try:
            print(mgr.ensure_running())
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    elif args.action == "status":
        print(mgr.status())
    elif args.action == "shutdown":
        mgr.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
