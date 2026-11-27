"""Which backend the daemon-managed llamafile embedder runs on (ch-33).

claude-hooks decides the backend, never the engine. An opencoti
llamafile started without ``--gpu`` picks one itself (Metal, CUDA, AMD,
then Vulkan), so every spawn names it:

- ``cpu``    -> ``--gpu disable``
- ``vulkan`` -> ``--gpu vulkan -dev <VulkanN> -ngl 99``, the device chosen
  by name (``embedding.device``: a ``VulkanN`` id or part of the name the
  engine lists, e.g. ``RENOIR``) and resolved against ``--list-devices``
  before each spawn, because the index moves when drivers change.
- ``cuda``   -> ``--gpu nvidia -dev <CUDAn> -ngl 99`` (needs a composite
  with the CUDA libraries), the device chosen the same way.
- ``auto``   -> the pre-ch-33 behaviour (``-ngl 99`` and the engine's own
  choice), kept only for configs that already say it.

A device can also be named by its PCI address (``embedding.pci``), the
one name that tells two identical cards apart and that CUDA and Vulkan
share for one card.

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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Sequence

BACKENDS = ("auto", "cpu", "vulkan", "cuda")

#: Backends a device list exists for: name -> (--gpu value, device prefix).
GPU_BACKENDS = {"vulkan": ("vulkan", "Vulkan"), "cuda": ("nvidia", "CUDA")}

#: Reference vectors for the shipped model (qwen3-embedding-0.6b Q8_0,
#: pooling last), computed with the opencoti composite on the CPU.
PROBE_REFERENCE = Path(__file__).with_name("embedder_probe_reference.json")

#: Below this cosine against the CPU reference a backend is wrong, not
#: noisy: a working Vulkan run scores >= 0.999, the broken one 0.38.
MIN_PROBE_COSINE = 0.99

_DEVICE_RX = re.compile(
    r"^\s*((?:Vulkan|CUDA|ROCm)\d+):\s*(.+?)\s*\((\d+) MiB(?:, (\d+) MiB free)?\)(.*)$",
    re.M)
_PCI_RX = re.compile(r"pci=([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])")


@dataclass(frozen=True)
class Device:
    """One device as the engine lists it."""
    id: str                      # Vulkan0, CUDA1
    name: str
    memory_mib: Optional[int] = None
    free_mib: Optional[int] = None
    pci: str = ""                # dddd:bb:dd.f, lower case; "" when not reported

    def as_dict(self) -> dict:
        return asdict(self)


def normalise_pci(addr: str) -> str:
    """``0000:2d:00.0`` form, lower case; a missing domain means 0000."""
    a = (addr or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]", a):
        a = "0000:" + a
    return a


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
    for key in ("VK_ICD_FILENAMES", "VK_DRIVER_FILES", "OPENCOTI_VK_ALLOW_OLD_DRIVER",
                "GGML_VK_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
        env.pop(key, None)
    # CUDA numbers devices fastest-first by default; in PCI order CUDAn is
    # nvidia-smi's index n, which is how cuda_pci() names their addresses.
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    if mode == "vulkan":
        if vulkan_icd:
            env["VK_ICD_FILENAMES"] = vulkan_icd
            env["VK_DRIVER_FILES"] = vulkan_icd
        if allow_old_driver:
            env["OPENCOTI_VK_ALLOW_OLD_DRIVER"] = "1"
    return env


def parse_devices(text: str) -> list[Device]:
    """Devices from ``--list-devices`` output (the engine's own ids)."""
    out = []
    for m in _DEVICE_RX.finditer(text):
        pci = _PCI_RX.search(m.group(5) or "")
        out.append(Device(id=m.group(1), name=m.group(2),
                          memory_mib=int(m.group(3)),
                          free_mib=int(m.group(4)) if m.group(4) else None,
                          pci=normalise_pci(pci.group(1)) if pci else ""))
    return out


def _named(devices) -> str:
    return ", ".join(f"{d.id} ({d.name}{', pci ' + d.pci if d.pci else ''})"
                     for d in devices)


def resolve_device(spec: str, devices: Sequence[Device], *, pci: str = "") -> Device:
    """The device ``spec`` (or ``pci``) names.

    ``pci`` wins when given and must match a listed address. ``spec`` is
    an id (``Vulkan1``) or part of a device name, case-insensitive; it
    must name exactly one device. Both empty means the only device
    there is. Refuses rather than guesses.
    """
    if not devices:
        raise BackendError("the engine lists no usable device for this backend")
    if pci:
        want = normalise_pci(pci)
        hits = [d for d in devices if d.pci == want]
        if hits:
            return hits[0]
        raise BackendError(f"no device at PCI {want}; the engine lists " + _named(devices))
    spec = (spec or "").strip()
    if not spec:
        if len(devices) == 1:
            return devices[0]
        raise BackendError("several devices and no device or pci is set: " + _named(devices))
    for d in devices:
        if d.id.lower() == spec.lower():
            return d
    hits = [d for d in devices if spec.lower() in d.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise BackendError(f"no device matches {spec!r}; the engine lists " + _named(devices))
    raise BackendError(f"{spec!r} matches several devices: " + _named(hits)
                       + " — name one by id or pci")


def list_devices(llamafile: str, env: dict, *, backend: str = "vulkan",
                 timeout: float = 120.0, platform: str = sys.platform) -> str:
    """Run ``<llamafile> --gpu <backend> --list-devices`` with ``env``."""
    cmd = [llamafile, "--gpu", GPU_BACKENDS[backend][0], "--list-devices"]
    if not platform.startswith("win"):
        cmd = ["/bin/sh", *cmd]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True,
                             timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackendError(f"--list-devices failed: {e}") from e
    return (out.stdout or "") + (out.stderr or "")


# --------------------------------------------------------------- PCI addresses
#
# ``--list-devices`` prints no PCI address on either engine backend (c14 r19,
# 2026-11-27); the running server's /props does (``opencoti.devices[].pci``).
# For the listing the host fills them in: nvidia-smi for CUDA (indexes match
# because child_env sets CUDA_DEVICE_ORDER=PCI_BUS_ID), sysfs for Vulkan by
# vendor when only one display device of that vendor exists. Anything not
# certain stays "".

_VENDORS = (("0x1002", ("amd", "radeon", "radv")),
            ("0x10de", ("nvidia", "geforce", "quadro", "tesla", "rtx")),
            ("0x8086", ("intel",)))


def parse_nvidia_smi(text: str) -> dict[int, str]:
    """``index, pci.bus_id`` csv rows -> {index: pci}."""
    out: dict[int, str] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        bus = parts[1].lower()
        dom, _, rest = bus.partition(":")
        if rest and len(dom) == 8:           # 00000000:01:00.0
            bus = dom[-4:] + ":" + rest
        out[int(parts[0])] = normalise_pci(bus)
    return out


def cuda_pci(*, timeout: float = 10.0) -> dict[int, str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_nvidia_smi(out.stdout or "") if out.returncode == 0 else {}


def display_devices(root: str = "/sys/bus/pci/devices") -> list[tuple[str, str]]:
    """(pci, vendor id) of every display-class PCI device (Linux sysfs)."""
    found = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        try:
            with open(os.path.join(root, name, "class"), encoding="ascii") as fh:
                cls = fh.read().strip()
            with open(os.path.join(root, name, "vendor"), encoding="ascii") as fh:
                vendor = fh.read().strip().lower()
        except OSError:
            continue
        if cls.startswith("0x03"):
            found.append((normalise_pci(name), vendor))
    return found


def _vendor_of(name: str) -> str:
    low = name.lower()
    for vid, words in _VENDORS:
        if any(w in low for w in words):
            return vid
    return ""


def with_pci(devices: list[Device], backend: str, *, smi: Optional[dict] = None,
             display: Optional[list] = None, platform: str = sys.platform) -> list[Device]:
    """Fill in the PCI address of each device the engine listed without one."""
    from dataclasses import replace
    if all(d.pci for d in devices):
        return devices
    out = []
    if backend == "cuda":
        smi = cuda_pci() if smi is None else smi
        for d in devices:
            m = re.fullmatch(r"CUDA(\d+)", d.id)
            pci = d.pci or (smi.get(int(m.group(1)), "") if m else "")
            out.append(replace(d, pci=pci))
        return out
    if backend == "vulkan":
        if display is None:
            display = display_devices() if platform.startswith("linux") else []
        for d in devices:
            vid = _vendor_of(d.name)
            hits = [p for p, v in display if v == vid] if vid else []
            same = [x for x in devices if _vendor_of(x.name) == vid]
            pci = d.pci or (hits[0] if len(hits) == 1 and len(same) == 1 else "")
            out.append(replace(d, pci=pci))
        return out
    return devices


_TCP_ESTABLISHED = "01"


def count_proc_net_tcp(text: str, port: int) -> int:
    """Established connections whose local port is ``port`` in the text of
    ``/proc/net/tcp`` or ``tcp6``."""
    n = 0
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 4 or f[3] != _TCP_ESTABLISHED:
            continue
        try:
            if int(f[1].rsplit(":", 1)[1], 16) == port:
                n += 1
        except (IndexError, ValueError):
            continue
    return n


def count_netstat(text: str, port: int) -> int:
    """Established connections with local port ``port`` in ``netstat -an``
    output (Windows: ``TCP  127.0.0.1:38092  127.0.0.1:50311  ESTABLISHED``)."""
    n = 0
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0].upper().startswith("TCP") \
                and f[3].upper() == "ESTABLISHED" and f[1].rsplit(":", 1)[-1] == str(port):
            n += 1
    return n


def open_connections(port: int, *, platform: str = sys.platform) -> Optional[int]:
    """Client connections open to the embed port right now, ``None`` when
    the host cannot tell.

    An embed request is visible here from the moment it connects; ``/slots``
    shows it only once a slot takes it, which for a long text is seconds
    later (14k characters read idle for ~3 s on a loaded CPU, 2026-11-27).
    Every embedder client here opens one connection per request.
    """
    if platform.startswith("linux"):
        total, seen = 0, False
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(name, encoding="ascii", errors="replace") as fh:
                    total += count_proc_net_tcp(fh.read(), port)
                seen = True
            except OSError:
                continue
        return total if seen else None
    if platform.startswith("win"):
        try:
            out = subprocess.run(["netstat", "-an", "-p", "TCP"], capture_output=True,
                                 text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return count_netstat(out.stdout or "", port)
    return None


def flags(mode: str, device_id: str = "") -> list[str]:
    """Backend arguments for one spawn. ``auto`` keeps the old flags."""
    if mode == "cpu":
        return ["--gpu", "disable"]
    if mode in GPU_BACKENDS:
        if not device_id:
            raise BackendError(f"{mode} backend without a resolved device")
        return ["--gpu", GPU_BACKENDS[mode][0], "-dev", device_id, "-ngl", "99"]
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
