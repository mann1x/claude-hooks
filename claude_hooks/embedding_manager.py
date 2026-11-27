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

import json
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

from claude_hooks import llamafile_backend

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

    # Effective context size **per slot**; must match what the
    # LlamafileEmbedder / CompositeEmbedder primary expects. The
    # composite shipped with claude-hooks is built with --ctx-size 16384
    # baked in but the `--ctx-size` CLI flag still takes precedence.
    #
    # NOTE the per-slot framing: llama.cpp's ``--ctx-size`` is the
    # *total* KV budget, divided evenly across ``--parallel`` slots. So
    # ``_build_cmd`` passes ``ctx_size * n_parallel``. Were ctx_size
    # taken as the total instead, raising n_parallel would silently
    # shrink every individual embed's window — turning a concurrency
    # win into truncated vectors, which is the failure mode that looks
    # like success.
    ctx_size: int = 16384

    # Number of llama.cpp server slots, i.e. how many embeds run
    # concurrently. **Do not lower this to 1.** The embedder serves two
    # callers with very different urgency: background turn-stores
    # (detached, nobody waiting) and interactive recall on the
    # UserPromptSubmit critical path (bounded by a hook timeout). With
    # one slot a 5-6 s background store sits directly in front of the
    # user's recall. Raised to 3 on solidpc 2026-09-09 after the store
    # gate was measured queueing p50 14.5 s / p90 39 s.
    #
    # This has been lost twice by being set on the running llamafile by
    # hand rather than in config — the daemon rebuilds the argv on every
    # respawn, so a manual `--parallel` survives only until the next
    # idle reap. It is a *default* here as well as a config key so that
    # a wiped or hand-trimmed config still gets 3.
    n_parallel: int = 3

    # Pooling mode; qwen3-embedding wants ``last``.
    pooling: str = "last"

    # Backend, decided here and never by the engine (ch-33; see
    # claude_hooks/llamafile_backend.py):
    #   "cpu"    -> --gpu disable on every spawn, never probes.
    #   "vulkan" -> --gpu vulkan -dev <device> -ngl 99, the device
    #               resolved by name before each spawn; the vectors are
    #               checked against a CPU reference after start, and any
    #               failure falls back to CPU for the daemon session.
    #   "auto"   -> the pre-ch-33 flags (-ngl 99, the engine picks the
    #               GPU); kept for configs that already say it.
    mode: str = "auto"

    # mode="vulkan"/"cuda": which device — an id (Vulkan0, CUDA1) or part
    # of the name `<llamafile> --gpu <backend> --list-devices` prints
    # (e.g. "RENOIR"). Empty = the only device listed; several -> refused.
    device: str = ""
    # Or by PCI address (dddd:bb:dd.f), which tells identical cards apart
    # and wins over ``device``.
    pci: str = ""
    # mode="vulkan": a Vulkan ICD manifest for the child only
    # (VK_ICD_FILENAMES), e.g. a private RADV build when the system
    # Mesa is too old. Empty = the system's drivers.
    vulkan_icd: str = ""
    # mode="vulkan": let the engine use a driver it knows to be broken
    # (OPENCOTI_VK_ALLOW_OLD_DRIVER=1). On solidpc's Mesa 20.3.5 that
    # gave wrong vectors with no error; the probe check catches it.
    allow_old_driver: bool = False
    # GPU backends: compare probe vectors with the shipped CPU reference
    # after each start (only for the shipped model).
    verify_backend: bool = True
    # Embedder control endpoint (HTTP, served by the daemon): list
    # backends/devices, switch the backend gracefully. Loopback by
    # default; on another address only GETs are answered for
    # non-loopback clients. 0 disables it.
    control_host: str = "127.0.0.1"
    control_port: int = 38091
    # Where a switched backend is kept across daemon restarts until
    # `reset`. Empty = ~/.claude/claude-hooks-embedder-backend.json.
    override_path: str = ""

    # When mode=auto, override the VRAM budget check; ignored when
    # mode=cpu.
    vram_budget_mb: int = _DEFAULT_VRAM_BUDGET_MB

    # Lifecycle knobs.
    # OS scheduling priority for the llamafile process. Negative = higher
    # priority on POSIX (requires root / CAP_SYS_NICE; a failure to apply
    # is logged and ignored, never fatal). The embedder is a latency-
    # critical *shared* service: an interactive recall on the
    # UserPromptSubmit critical path is bounded by a hook timeout, and on
    # a box also running heavy local inference the embedder otherwise
    # competes as an equal with minutes-long batch jobs. -5 puts it ahead
    # of default-priority CPU hogs without the starvation risk of a
    # real-time class. 0 disables the adjustment.
    nice: int = -5
    spawn_timeout_seconds: float = 30.0
    # 3600, not 300: spawn is only 1-2 s, so reaping aggressively buys
    # little, but every reap opens a respawn race that concurrent
    # sessions observe as "the embedder is down" (the complaint clears
    # on the next attempt, once warm). Keeping the ~1.5 GB resident for
    # an hour is the cheaper trade. Was 300 (Ollama OLLAMA_KEEP_ALIVE
    # parity); see feedback_embedder_timeout_chain, solidpc 2026-07-25.
    idle_timeout_seconds: float = 3600.0
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
        # Why the session fell back to CPU (empty while it has not).
        self.fallback_reason: str = ""
        # The resolved device of the last spawn (GPU backends).
        self.active_device: Optional[llamafile_backend.Device] = None
        # The backend in force: a switched one (override file) or config.
        self.override: Optional[dict] = self._load_override()
        self._switch_lock = threading.Lock()
        self._backends_cache: Optional[tuple[float, dict]] = None
        self._stop_reaper = threading.Event()
        self._reaper_thread: Optional[threading.Thread] = None
        # Sanity-check pooling at construct time — silently dropping
        # an invalid value at spawn time would be very confusing.
        if cfg.pooling not in _VALID_POOLING:
            raise ValueError(
                f"invalid pooling mode {cfg.pooling!r}; "
                f"expected one of {sorted(_VALID_POOLING)}"
            )
        if self.override and self.override.get("mode") not in llamafile_backend.BACKENDS:
            log.warning("ignoring embedder override with mode %r",
                        self.override.get("mode"))
            self.override = None
        if cfg.mode not in llamafile_backend.BACKENDS:
            raise ValueError(
                f"invalid mode {cfg.mode!r}; expected one of "
                f"{', '.join(llamafile_backend.BACKENDS)}"
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
                "device": (self.active_device.as_dict()
                           if self.active_device and self._effective_mode()
                           in llamafile_backend.GPU_BACKENDS else None),
                "fallback_reason": self.fallback_reason,
                "backend": self.backend(),
            }

    # ----- spawn / health --------------------------------------- #

    # ----- backend -------------------------------------------------- #

    def backend(self) -> dict:
        """The backend in force: a switched one, else the config's."""
        if self.override:
            o = self.override
            return {"mode": o.get("mode"), "device": o.get("device") or "",
                    "pci": o.get("pci") or "", "source": "switched",
                    "reason": o.get("reason") or "", "set_at": o.get("set_at")}
        return {"mode": self.cfg.mode, "device": self.cfg.device, "pci": self.cfg.pci,
                "source": "config", "reason": "", "set_at": None}

    def _override_file(self) -> str:
        return self.cfg.override_path or os.path.join(
            os.path.expanduser("~"), ".claude", "claude-hooks-embedder-backend.json")

    def _load_override(self) -> Optional[dict]:
        try:
            with open(self._override_file(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _store_override(self, data: Optional[dict]) -> None:
        path = self._override_file()
        if data is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def _effective_mode(self) -> str:
        """The backend the next spawn uses.

        ``cpu`` when the backend in force is cpu OR a GPU start fell
        back this session; otherwise the backend in force.
        """
        mode = self.backend()["mode"]
        if mode == "cpu" or self.gpu_offload_failed:
            return "cpu"
        return mode

    def _child_env(self, mode: Optional[str] = None) -> dict:
        return llamafile_backend.child_env(
            mode=mode or self._effective_mode(), vulkan_icd=self.cfg.vulkan_icd,
            allow_old_driver=self.cfg.allow_old_driver,
        )

    def _fall_back(self, reason: str) -> None:
        """CPU for the rest of the daemon session. Caller holds the lock."""
        log.warning("embedding backend %s unusable (%s); using CPU until the "
                    "next switch or restart", self.backend()["mode"], reason)
        self.gpu_offload_failed = True
        self.fallback_reason = reason

    def _devices(self, mode: str) -> list:
        out = llamafile_backend.list_devices(
            self.cfg.llamafile_path, self._child_env(mode), backend=mode)
        return llamafile_backend.with_pci(llamafile_backend.parse_devices(out), mode)

    def _confirm_pci(self) -> None:
        """Take the running device's PCI address from the server's /props
        (``opencoti.devices[].pci``), which the engine knows for certain.
        Soft: an older engine without the field changes nothing."""
        d = self.active_device
        if d is None or self._effective_mode() not in llamafile_backend.GPU_BACKENDS:
            return
        try:
            with urllib.request.urlopen(self._url("/props"), timeout=3.0) as r:
                props = json.loads(r.read())
        except Exception:
            return
        for dev in (props.get("opencoti") or {}).get("devices") or []:
            if isinstance(dev, dict) and dev.get("name") == d.id and dev.get("pci"):
                from dataclasses import replace
                self.active_device = replace(
                    d, pci=llamafile_backend.normalise_pci(str(dev["pci"])))
                return

    def _resolve_device(self) -> None:
        """Resolve the GPU device for the next spawn, or fall back to
        CPU. Caller holds the lock."""
        b = self.backend()
        try:
            self.active_device = llamafile_backend.resolve_device(
                b["device"], self._devices(b["mode"]), pci=b["pci"])
        except llamafile_backend.BackendError as e:
            self.active_device = None
            self._fall_back(str(e))

    def _backend_verified(self) -> bool:
        """GPU backend: the probe vectors match the CPU reference.

        Skipped (True) for a custom GGUF, with ``verify_backend`` off,
        or when no reference ships. Caller holds the lock."""
        if (self._effective_mode() not in llamafile_backend.GPU_BACKENDS
                or not self.cfg.verify_backend or self.cfg.model_gguf):
            return True
        ref = llamafile_backend.load_reference()
        if ref is None:
            log.warning("no embedder probe reference; GPU vectors unchecked")
            return True
        ok, detail = llamafile_backend.check_vectors(self._url("/embedding"), ref)
        log.info("embedding backend check on %s: %s",
                 self.active_device.id if self.active_device else "?", detail)
        if not ok:
            self._fall_back(detail)
        return ok

    def _url(self, path: str) -> str:
        host = "127.0.0.1" if self.cfg.host in ("0.0.0.0", "::", "") else self.cfg.host
        return f"http://{host}:{self.cfg.port}{path}"

    def _gpu_flags(self) -> list[str]:
        """Backend arguments for the next spawn.

        ``cpu`` and ``vulkan`` are explicit (llamafile_backend.flags);
        ``auto`` keeps the pre-ch-33 decision. Calls
        :mod:`claude_hooks.gpu_probe` lazily so unit tests can stub it
        out without importing nvidia-smi machinery.
        """
        mode = self._effective_mode()
        if mode == "cpu" or mode in llamafile_backend.GPU_BACKENDS:
            return llamafile_backend.flags(
                mode, self.active_device.id if self.active_device else "")
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
        # See EmbeddingConfig.ctx_size: llama.cpp splits --ctx-size
        # across --parallel slots, so the total has to be scaled to keep
        # each slot at the configured per-slot window.
        slots = max(1, int(self.cfg.n_parallel))
        cmd = [
            self.cfg.llamafile_path,
            "--server",
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "--embedding",
            "--pooling", self.cfg.pooling,
            "--ctx-size", str(self.cfg.ctx_size * slots),
            "--parallel", str(slots),
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

        Windows detachment: ``start_new_session=True`` is a POSIX
        knob and silently no-ops on Windows. To prevent the spawned
        llamafile from inheriting (or creating) a visible console
        window we pass ``CREATE_NO_WINDOW | DETACHED_PROCESS`` in
        ``creationflags`` — the same pair the rest of claude-hooks
        uses for detached background processes
        (`claudemem_reindex._spawn_reindex`, `lsp_engine.client`).
        Stdin is wired to DEVNULL because a detached process has no
        usable stdin handle and inheriting one can pin a console
        alive.
        """
        cmd = self._build_cmd()
        log.info("spawning llamafile: %s", " ".join(cmd))
        wrapped = self._maybe_wrap_for_ape(cmd)
        kwargs: dict = {
            "cwd": cwd or None,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": self._child_env(),
        }
        if sys.platform.startswith("win"):
            # CREATE_NO_WINDOW suppresses the console allocation that
            # would otherwise appear when the daemon (which itself may
            # be running under a scheduled task with a console) spawns
            # a child. DETACHED_PROCESS severs the inherited console
            # entirely so the child survives the parent's window
            # being closed by the user.
            flags = (
                subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
            # Windows has no nice(2); express the same intent with a
            # priority class. Only the "raise it" direction is mapped —
            # a positive nice would mean BELOW_NORMAL, which we never
            # want for a latency-critical shared service.
            if self.cfg.nice < 0:
                flags |= getattr(
                    subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0,
                )
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(wrapped, **kwargs)
        self._apply_priority(proc)
        return proc

    def _apply_priority(self, proc: subprocess.Popen) -> None:
        """Nudge the llamafile's OS scheduling priority (POSIX).

        Applied *after* spawn rather than via ``preexec_fn`` — the
        latter is documented-unsafe in a threaded parent, and the daemon
        is threaded. The PID is stable across the APE ``/bin/sh``
        bootstrap because that shell ``exec``s the real program in
        place, so adjusting ``proc.pid`` reaches the actual server.

        Best-effort by design: lowering niceness needs root or
        CAP_SYS_NICE, and the embedder must still start for an
        unprivileged user. Windows is handled at creation time via the
        priority class, so this is a POSIX-only no-op there.
        """
        if sys.platform.startswith("win") or not self.cfg.nice:
            return
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, self.cfg.nice)
            log.debug("llamafile pid=%s nice set to %d",
                      proc.pid, self.cfg.nice)
        except (OSError, PermissionError, AttributeError) as e:
            log.debug(
                "could not set llamafile nice to %d (needs root/CAP_SYS_NICE): %s",
                self.cfg.nice, e,
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
            if self._effective_mode() in llamafile_backend.GPU_BACKENDS:
                self._resolve_device()
            try:
                self.proc = self._spawn_once(cwd)
            except OSError as e:
                raise RuntimeError(
                    f"llamafile spawn failed ({e}); "
                    f"check that {self.cfg.llamafile_path!r} is "
                    "executable and the platform is supported."
                ) from e

            healthy = self._wait_for_health()
            if healthy and self._backend_verified():
                self.last_activity_at = time.time()
                self._confirm_pci()
                log.info(
                    "llamafile ready on port %d (pid=%s, mode=%s%s)",
                    self.cfg.port, self.proc.pid, self._effective_mode(),
                    f", device={self.active_device.id}"
                    if self._effective_mode() in llamafile_backend.GPU_BACKENDS
                    and self.active_device else "",
                )
                return {
                    "ready": True,
                    "port": self.cfg.port,
                    "mode": self._effective_mode(),
                    "spawned": True,
                    **({"gpu_fallback": True, "fallback_reason": self.fallback_reason}
                       if self.gpu_offload_failed and self.backend()["mode"] != "cpu"
                       else {}),
                }

            # Health never returned 200, or the GPU's vectors failed the
            # probe check (which already recorded the fall-back). If we
            # were trying a GPU path, fall back to CPU once. Any other
            # failure surfaces as a hard error so the CompositeEmbedder
            # can fail over to its primary.
            self._terminate_locked()
            if self.backend()["mode"] in ("auto", *llamafile_backend.GPU_BACKENDS) and (
                    not self.gpu_offload_failed or healthy):
                if not healthy:
                    self._fall_back(
                        f"no health on {self.backend()['mode']} within "
                        f"{self.cfg.spawn_timeout_seconds:.1f}s")
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
                        "fallback_reason": self.fallback_reason,
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

    # ----- control: list backends, switch gracefully -------------- #

    #: How long a device list stays fresh. Listing loads the GPU library
    #: (seconds); devices change only with hardware or drivers.
    BACKENDS_TTL_S = 300.0
    #: Two idle /slots reads this far apart make the embedder idle.
    IDLE_GAP_S = 0.25

    def backends(self, *, refresh: bool = False) -> dict:
        """What this composite can run on here, with each device's id,
        name, memory and PCI address, plus the backend in force."""
        now = time.time()
        if (not refresh and self._backends_cache
                and now - self._backends_cache[0] < self.BACKENDS_TTL_S):
            listed = self._backends_cache[1]
        else:
            listed = {"cpu": {"available": True, "devices": []}}
            for mode in llamafile_backend.GPU_BACKENDS:
                try:
                    devs = self._devices(mode)
                    listed[mode] = {"available": bool(devs),
                                    "devices": [d.as_dict() for d in devs]}
                    if not devs:
                        listed[mode]["reason"] = (
                            "the engine lists no usable device (no driver, a refused "
                            "driver, or this composite has no library for it)")
                except llamafile_backend.BackendError as e:
                    listed[mode] = {"available": False, "devices": [], "reason": str(e)}
            self._backends_cache = (now, listed)
        st = self.status()
        return {"backends": listed, "listed_at": self._backends_cache[0],
                "current": {k: st.get(k) for k in (
                    "alive", "mode", "device", "backend", "fallback_reason")}}

    def _busy(self) -> bool:
        """True while the embedder has work a restart would cut.

        Three signals, any one is enough:

        * a client connection open to the embed port
          (:func:`llamafile_backend.open_connections`) — a request is
          visible there from the moment it connects, while ``/slots``
          reads idle until a slot takes it (~3 s for a 14k-character text
          on a loaded CPU; the first live switch cut such a request);
        * a slot ``is_processing``;
        * ``/slots`` not answering in time while the server is up: the
          mozilla engine answers it from the inference loop, so it stalls
          for the length of an embed (``embedder_gate``).

        A server that refuses the connection is not busy: there is nothing
        to interrupt.
        """
        conns = llamafile_backend.open_connections(self.cfg.port)
        if conns:
            return True
        try:
            with urllib.request.urlopen(self._url("/slots"), timeout=2.0) as r:
                slots = json.loads(r.read())
        except (socket.timeout, TimeoutError):
            return True
        except urllib.error.URLError as e:
            return isinstance(e.reason, (socket.timeout, TimeoutError))
        except Exception:
            return False
        return any(s.get("is_processing") for s in slots if isinstance(s, dict))

    def _wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._busy():
                time.sleep(self.IDLE_GAP_S)
                if not self._busy():
                    return True
            time.sleep(0.5)
        return False

    def switch(self, mode: str, *, device: str = "", pci: str = "",
               reason: str = "", persist: bool = True,
               idle_timeout: float = 120.0, force: bool = False) -> dict:
        """Move the embedder to another backend, gracefully.

        The device is resolved first, so a switch to something that is
        not there changes nothing. A running server is restarted only
        once every slot is idle (two reads ``IDLE_GAP_S`` apart); with
        ``force`` it is restarted after ``idle_timeout`` regardless.
        The new backend is checked like any start: a GPU whose vectors
        fail the probe falls back to CPU and the answer says so.
        ``persist`` keeps the choice across daemon restarts until
        :meth:`reset`. A stopped server is not started: the next embed
        starts it on the new backend.
        """
        if mode not in ("cpu", *llamafile_backend.GPU_BACKENDS):
            return {"ok": False, "changed": False,
                    "error": f"cannot switch to {mode!r}; expected cpu, "
                    + ", ".join(llamafile_backend.GPU_BACKENDS)}
        target = None
        if mode in llamafile_backend.GPU_BACKENDS:
            try:
                target = llamafile_backend.resolve_device(
                    device, self._devices(mode), pci=pci)
            except llamafile_backend.BackendError as e:
                return {"ok": False, "error": str(e), "changed": False}
        override = {"mode": mode, "device": target.id if target else "",
                    "pci": target.pci if target else "", "reason": reason,
                    "set_at": time.time()}
        return self._restart_into(override, persist=persist,
                                  idle_timeout=idle_timeout, force=force)

    def reset(self, *, idle_timeout: float = 120.0, force: bool = False) -> dict:
        """Back to the configured backend; forgets a switched one."""
        return self._restart_into(None, persist=True,
                                  idle_timeout=idle_timeout, force=force)

    def _restart_into(self, override: Optional[dict], *, persist: bool,
                      idle_timeout: float, force: bool) -> dict:
        """Wait for idle, stop, put ``override`` in force (None = the
        config), restart if it was running. ``persist`` writes the
        override file (or removes it for None)."""
        if not self._switch_lock.acquire(blocking=False):
            return {"ok": False, "changed": False,
                    "error": "another switch is in progress"}
        try:
            was_alive = self.alive()
            waited = time.monotonic()
            idle = True
            if was_alive:
                idle = self._wait_idle(idle_timeout)
                if not idle and not force:
                    return {"ok": False, "changed": False, "error":
                            f"the embedder stayed busy for {idle_timeout:g} s; "
                            "retry, or pass force"}
            with self.lock:
                self._terminate_locked()
                self.override = override
                self.gpu_offload_failed = False
                self.fallback_reason = ""
                self.active_device = None
                if persist:
                    self._store_override(override)
            b = self.backend()
            log.info("embedder backend now %s %s%s (%s)", b["mode"], b["device"],
                     f" pci={b['pci']}" if b["pci"] else "",
                     (override or {}).get("reason") or b["source"])
            result = {"ok": True, "changed": True, "restarted": was_alive,
                      "waited_s": round(time.monotonic() - waited, 2),
                      "forced": was_alive and not idle}
            if was_alive:
                try:
                    result["start"] = self.ensure_running()
                except RuntimeError as e:
                    result.update(ok=False, error=f"restart failed: {e}")
            result["status"] = self.status()
            return result
        finally:
            self._switch_lock.release()

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
        # ``or 3`` deliberately: a config carrying 0 (or the key wiped
        # by an install.py run that predates it) gets the working
        # default rather than a single-slot embedder.
        n_parallel=int(e.get("n_parallel") or 3),
        pooling=str(e.get("pooling") or "last"),
        mode=str(e.get("mode") or "auto"),
        device=str(e.get("device") or ""),
        pci=str(e.get("pci") or ""),
        control_host=str(e.get("control_host") or "127.0.0.1"),
        control_port=int(e.get("control_port", 38091) or 0),
        override_path=os.path.expanduser(str(e.get("override_path") or "")),
        vulkan_icd=os.path.expanduser(str(e.get("vulkan_icd") or "")),
        allow_old_driver=bool(e.get("allow_old_driver", False)),
        verify_backend=bool(e.get("verify_backend", True)),
        vram_budget_mb=int(e.get("vram_budget_mb") or _DEFAULT_VRAM_BUDGET_MB),
        nice=int(e.get("nice", -5)),
        spawn_timeout_seconds=float(e.get("spawn_timeout_seconds") or 30.0),
        idle_timeout_seconds=float(e.get("idle_timeout_seconds") or 3600.0),
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
    parser.add_argument("--mode", choices=llamafile_backend.BACKENDS, default="auto")
    parser.add_argument("--device", default="")
    parser.add_argument("--pci", default="")
    parser.add_argument("--vulkan-icd", default="")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = EmbeddingConfig(
        llamafile_path=args.llamafile, port=args.port, mode=args.mode,
        device=args.device, pci=args.pci, vulkan_icd=args.vulkan_icd,
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
