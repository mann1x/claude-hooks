"""Tiny GPU detection helper used at install time and at runtime.

Used by two callers:

1. ``install.py`` (``_setup_llamafile_engine``) — to decide whether
   the ``mode=auto`` default makes sense for this host or whether
   the installer should suggest ``mode=cpu`` upfront (e.g. on a
   GPU-less server).
2. :mod:`claude_hooks.embedding_manager` (runtime) — on each cold
   spawn of the llamafile, to decide whether to pass ``-ngl 99`` (full
   GPU offload) or ``--gpu disable`` (CPU-only). Runs once per spawn,
   not per embed call, so the subprocess cost is negligible.

The result is the union of:

- ``vendor`` — ``"nvidia"`` | ``"amd"`` | ``"vulkan"`` | ``"none"``
- ``free_mb`` / ``total_mb`` — only set for nvidia and amd paths
  (vulkaninfo doesn't expose VRAM in its summary mode)
- ``raw`` — the first non-empty line of the underlying tool, kept
  for diagnostics

The probe is deliberately pessimistic — any timeout, exception, or
unparseable output yields the next backend, ultimately falling to
``{"vendor": "none"}``. False positives would mislead the caller
into asking the runtime to offload to a GPU that doesn't exist.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

# Bounded timeout for every probe — these tools are local CLI binaries,
# so anything over a couple of seconds is a sign they're stuck (e.g.
# nvidia-smi hanging on a busted driver, vulkaninfo on a broken
# display server).
_PROBE_TIMEOUT_S: float = 2.0


def _run(cmd: list[str]) -> Optional[str]:
    """Run ``cmd`` with a hard timeout, return stdout or ``None`` on
    any failure (missing binary, non-zero exit, timeout, decode error).
    """
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _probe_nvidia() -> Optional[dict]:
    """``nvidia-smi --query-gpu=memory.total,memory.free
    --format=csv,noheader,nounits`` outputs one line per GPU,
    each like ``24576, 22134``. We aggregate across GPUs (sum)
    so a multi-GPU box reports its total available VRAM, which
    is what the llamafile runtime can actually use."""
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return None
    total = 0
    free = 0
    first_line = ""
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if not first_line:
            first_line = line
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            return None
        try:
            total += int(parts[0])
            free += int(parts[1])
        except ValueError:
            return None
    if total <= 0:
        return None
    return {
        "vendor": "nvidia",
        "total_mb": total,
        "free_mb": free,
        "raw": first_line,
    }


def _probe_amd() -> Optional[dict]:
    """``rocm-smi --showmeminfo vram --json`` returns a JSON blob keyed
    by ``card0``, ``card1``, …; each entry has ``VRAM Total Memory (B)``
    and ``VRAM Total Used Memory (B)``. We aggregate across cards in
    the same shape as the nvidia probe."""
    import json as _json

    out = _run(["rocm-smi", "--showmeminfo", "vram", "--json"])
    if not out:
        return None
    try:
        data = _json.loads(out)
    except (ValueError, _json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    total_b = 0
    used_b = 0
    seen = False
    for card, info in data.items():
        if not card.startswith("card"):
            continue
        if not isinstance(info, dict):
            continue
        # rocm-smi has two label conventions across versions; try both.
        t_key = next(
            (k for k in info if "VRAM Total Memory" in k and "Used" not in k),
            None,
        )
        u_key = next(
            (k for k in info if "VRAM Total Used Memory" in k),
            None,
        )
        if not t_key or not u_key:
            continue
        try:
            total_b += int(info[t_key])
            used_b += int(info[u_key])
            seen = True
        except (TypeError, ValueError):
            continue
    if not seen or total_b <= 0:
        return None
    total_mb = total_b // (1024 * 1024)
    free_mb = max(0, total_mb - (used_b // (1024 * 1024)))
    return {
        "vendor": "amd",
        "total_mb": total_mb,
        "free_mb": free_mb,
        "raw": "rocm-smi",
    }


def _probe_vulkan() -> Optional[dict]:
    """``vulkaninfo --summary`` prints a per-device block. We don't
    extract VRAM here — vulkaninfo's summary mode doesn't report it
    reliably across drivers. Presence of a non-CPU device is enough
    to know the GPU path is at least theoretically open; the caller
    treats ``vulkan`` as "auto-mode is fine but we can't pre-flight
    VRAM"."""
    out = _run(["vulkaninfo", "--summary"])
    if not out:
        return None
    # The summary lists "deviceType = PHYSICAL_DEVICE_TYPE_..." lines;
    # any DISCRETE_GPU / INTEGRATED_GPU / VIRTUAL_GPU counts.
    saw_gpu = False
    raw_line = ""
    for line in out.splitlines():
        s = line.strip()
        if "deviceType" not in s:
            continue
        if "GPU" in s:
            saw_gpu = True
            if not raw_line:
                raw_line = s
            break
    if not saw_gpu:
        return None
    return {
        "vendor": "vulkan",
        "total_mb": None,
        "free_mb": None,
        "raw": raw_line,
    }


def probe() -> dict:
    """Run the probe chain. First hit wins; never raises.

    Return shape::

        {"vendor": "nvidia"|"amd"|"vulkan"|"none",
         "total_mb": int|None,
         "free_mb": int|None,
         "raw": str|None}
    """
    for fn in (_probe_nvidia, _probe_amd, _probe_vulkan):
        try:
            result = fn()
        except Exception:
            # The individual probe layers already handle their own
            # failures, but a defensive guard here keeps the public
            # contract ("never raises") true even if a probe is
            # extended carelessly.
            continue
        if result:
            return result
    return {"vendor": "none", "total_mb": None, "free_mb": None, "raw": None}


def has_gpu() -> bool:
    """Convenience: ``True`` if ``probe()["vendor"] != "none"``.

    Use ``probe()`` directly when you need the VRAM numbers (e.g. to
    decide whether the model + ctx will fit before passing
    ``-ngl 99``)."""
    return probe().get("vendor") != "none"


def can_fit_in_vram(required_mb: int, *, headroom_mb: int = 512) -> Optional[bool]:
    """Best-effort "does this model fit?" check.

    Returns ``True`` if ``free_mb >= required_mb + headroom_mb``,
    ``False`` if it doesn't, and ``None`` when we can't tell (vulkan
    or vendor=none). The EmbeddingManager uses this to pick
    ``-ngl 99`` only when we're sure the offload will succeed —
    otherwise it falls back to CPU spawn rather than risk an OOM
    crash loop.
    """
    p = probe()
    free = p.get("free_mb")
    if not isinstance(free, int):
        return None
    return free >= required_mb + headroom_mb
