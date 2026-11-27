"""Which backend the daemon-managed llamafile embedder runs on (ch-33).

claude-hooks decides the backend, never the engine. An opencoti
llamafile started without ``--gpu`` picks one itself (Metal, CUDA, AMD,
then Vulkan), so every spawn names it:

- ``cpu``    -> ``--gpu disable``
- ``vulkan`` -> ``--gpu vulkan -dev <VulkanN> -ngl 99``, the device chosen
  by name (``embedding.device``: a ``VulkanN`` id or part of the name the
  engine lists, e.g. ``RENOIR``) and resolved against ``--list-devices``
  before each spawn, because the index moves when drivers change.
- ``auto``   -> the pre-ch-33 behaviour (``-ngl 99`` and the engine's own
  choice), kept only for configs that already say it.

Three things measured on solidpc (2026-11-27) shape the rest:

1. A Vulkan driver can return wrong vectors without an error: RADV from
   Mesa 20.3.5 gave cosine 0.38 against the CPU. ``check_vectors()``
   compares a probe against a reference computed on the CPU and the
   manager falls back to CPU when it does not match.
2. Under the daemon's sandbox the engine cannot unpack its bundled
   ``ggml-vulkan`` library next to the binary or in ``~/.llamafile``, and
   silently loads whatever older copy it finds in ``~/.llamafile``.
   ``child_env()`` gives the child its own ``HOME`` and ``TMPDIR`` under
   ``~/.claude`` (writable in the sandbox), where it unpacks its own copy.
3. A driver can be chosen per process with ``VK_ICD_FILENAMES``
   (``embedding.vulkan_icd``), e.g. a private RADV build when the system
   Mesa is too old; the engine refuses a driver it knows to be broken
   unless ``embedding.allow_old_driver`` is set.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

BACKENDS = ("auto", "cpu", "vulkan")

#: Reference vectors for the shipped model (qwen3-embedding-0.6b Q8_0,
#: pooling last), computed with the opencoti composite on the CPU.
PROBE_REFERENCE = Path(__file__).with_name("embedder_probe_reference.json")

#: Below this cosine against the CPU reference a backend is wrong, not
#: noisy: a working Vulkan run scores >= 0.999, the broken one 0.38.
MIN_PROBE_COSINE = 0.99

_DEVICE_RX = re.compile(r"^\s*(Vulkan\d+):\s*(.+?)\s*\(\d+ MiB", re.M)


class BackendError(RuntimeError):
    """The configured backend cannot be used; the caller falls back."""


def runtime_dir(home: Optional[str] = None) -> Path:
    """Per-host scratch for the llamafile child (its HOME and TMPDIR)."""
    base = Path(home or os.path.expanduser("~")) / ".claude" / "claude-hooks-llamafile"
    return base


def child_env(*, mode: str, vulkan_icd: str = "", allow_old_driver: bool = False,
              base: Optional[dict] = None, home: Optional[str] = None,
              platform: str = sys.platform) -> dict:
    """Environment for the llamafile child.

    On POSIX the child gets a private HOME and TMPDIR (see module doc,
    point 2); Cosmopolitan also builds its dlopen helper in TMPDIR. The
    Vulkan knobs are set only for the ``vulkan`` backend and removed
    otherwise, so a value in the daemon's own environment never decides.
    """
    env = dict(os.environ if base is None else base)
    if not platform.startswith("win"):
        rt = runtime_dir(home)
        for sub in ("home", "tmp"):
            (rt / sub).mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(rt / "home")
        env["TMPDIR"] = str(rt / "tmp")
    for key in ("VK_ICD_FILENAMES", "VK_DRIVER_FILES", "OPENCOTI_VK_ALLOW_OLD_DRIVER"):
        env.pop(key, None)
    if mode == "vulkan":
        if vulkan_icd:
            env["VK_ICD_FILENAMES"] = vulkan_icd
            env["VK_DRIVER_FILES"] = vulkan_icd
        if allow_old_driver:
            env["OPENCOTI_VK_ALLOW_OLD_DRIVER"] = "1"
    return env


def parse_devices(text: str) -> list[tuple[str, str]]:
    """``[(id, name), ...]`` from ``--list-devices`` output."""
    return [(m.group(1), m.group(2)) for m in _DEVICE_RX.finditer(text)]


def resolve_device(spec: str, devices: Sequence[tuple[str, str]]) -> str:
    """The ``VulkanN`` id ``spec`` names.

    ``spec`` is an id (``Vulkan1``) or part of a device name, matched
    case-insensitively; it must name exactly one device. Empty means the
    only device there is. Refuses rather than guesses.
    """
    if not devices:
        raise BackendError("the engine lists no usable Vulkan device")
    spec = (spec or "").strip()
    if not spec:
        if len(devices) == 1:
            return devices[0][0]
        raise BackendError(
            "several Vulkan devices and embedding.device is not set: "
            + ", ".join(f"{i} ({n})" for i, n in devices))
    for dev_id, _ in devices:
        if dev_id.lower() == spec.lower():
            return dev_id
    hits = [(i, n) for i, n in devices if spec.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0][0]
    if not hits:
        raise BackendError(
            f"no Vulkan device matches {spec!r}; the engine lists "
            + ", ".join(f"{i} ({n})" for i, n in devices))
    raise BackendError(
        f"{spec!r} matches several Vulkan devices: "
        + ", ".join(f"{i} ({n})" for i, n in hits))


def list_devices(llamafile: str, env: dict, *, timeout: float = 120.0,
                 platform: str = sys.platform) -> str:
    """Run ``<llamafile> --gpu vulkan --list-devices`` with ``env``."""
    cmd = [llamafile, "--gpu", "vulkan", "--list-devices"]
    if not platform.startswith("win"):
        cmd = ["/bin/sh", *cmd]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True,
                             timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackendError(f"--list-devices failed: {e}") from e
    return (out.stdout or "") + (out.stderr or "")


def flags(mode: str, device_id: str = "") -> list[str]:
    """Backend arguments for one spawn. ``auto`` keeps the old flags."""
    if mode == "cpu":
        return ["--gpu", "disable"]
    if mode == "vulkan":
        if not device_id:
            raise BackendError("vulkan backend without a resolved device")
        return ["--gpu", "vulkan", "-dev", device_id, "-ngl", "99"]
    return ["-ngl", "99"]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def load_reference(path: Path = PROBE_REFERENCE) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _embed(url: str, text: str, timeout: float) -> list[float]:
    req = urllib.request.Request(url, data=json.dumps({"content": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if isinstance(d, list):
        d = d[0]
    e = d["embedding"]
    return e[0] if e and isinstance(e[0], list) else e


def check_vectors(url: str, reference: dict, *, min_cos: float = MIN_PROBE_COSINE,
                  timeout: float = 60.0, embed=_embed) -> tuple[bool, str]:
    """Embed the reference texts at ``url`` and compare.

    Returns ``(ok, detail)``; never raises. A failed request is not ok.
    """
    worst = 1.0
    try:
        for text, ref in zip(reference["texts"], reference["vectors"]):
            vec = embed(url, text, timeout)
            if len(vec) != len(ref) or not all(math.isfinite(x) for x in vec):
                return False, f"probe vector has dim {len(vec)}, want {len(ref)}"
            worst = min(worst, _cosine(vec, ref))
    except Exception as e:  # noqa: BLE001 - any failure means "not usable"
        return False, f"probe request failed: {e}"
    if worst < min_cos:
        return False, f"probe cosine {worst:.4f} < {min_cos} against the CPU reference"
    return True, f"probe cosine {worst:.5f}"
