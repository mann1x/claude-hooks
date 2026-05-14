"""Registry for llamafile chat models (v1.5+).

The registry maps a user-chosen **label** to a GGUF path + a handful
of supervision knobs (port, ctx_size, GPU mode, idle timeout). It
lives in ``~/.claude/llamafile-models.json`` as host-state — never in
the repo, never in ``config/claude-hooks.json`` — so multiple
claude-hooks installs on the same machine can share it and a repo
clone wipe doesn't lose the user's registered models.

Why a separate file rather than a block in claude-hooks.json
------------------------------------------------------------

The v1.4 ``embedding`` block in claude-hooks.json is a **singleton**:
one model, one port, one set of knobs. Chat models are a **registry**:
multiple models (HyDE wants a small one, consultants wants a big one,
get-advice may swap mid-session). Different shape → different file.

It also keeps the CLI tool (``claude-hooks-models``) ownership clean:
the CLI never touches claude-hooks.json, and install.py never has to
serialise/deserialise the registry. ``ChatModelManager`` loads this
file at daemon startup and reloads it on ensure-time if the file's
mtime changed (so a ``claude-hooks-models add`` followed by a fresh
ensure picks up the new entry without a daemon restart).

Layout (schema v1)
------------------

::

  {
    "schema_version": 1,
    "max_concurrent_loaded": 2,
    "default_port_range": [38093, 38099],
    "default_idle_timeout_seconds": 600,
    "default_mode": "auto",
    "models": {
      "<label>": {
        "gguf_path": "/abs/path/to/model.gguf",
        "ctx_size": 16384,
        "port": 38093,
        "mode": "auto",                # "auto" | "cpu"
        "idle_timeout_seconds": 600,
        "label_aliases": [],           # reserved for future
        "added_at": "2026-05-15T10:00:00Z",
        "notes": ""                    # free-text, CLI --notes
      },
      ...
    }
  }

Stdlib only — no external dependencies. Atomic save via
``<path>.tmp`` + ``os.replace`` matches the ``hyde_cache.py`` pattern
the rest of the project uses for host-state JSON files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.chat_model_registry")


# --------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------- #

DEFAULT_REGISTRY_PATH = Path.home() / ".claude" / "llamafile-models.json"

SCHEMA_VERSION = 1

# Port range carved out for chat models. Adjacent to the v1.4 embedding
# port 38092 and the consultants 38095/38096 — keep claude-hooks ports
# clustered for firewall/audit visibility.
DEFAULT_PORT_RANGE = (38093, 38099)

# Chat models are typically larger and slower to reload than the
# embedding model, so default idle window is longer (10 min vs 5 min).
DEFAULT_IDLE_TIMEOUT_SECONDS = 600

# Concurrency cap. Two loaded models covers the common case (small
# HyDE + big consultants); LRU evicts the third. Tunable per-host
# in the registry top-level.
DEFAULT_MAX_CONCURRENT_LOADED = 2

DEFAULT_MODE = "auto"

# Label charset: URL-safe + shell-safe + short. Lowercase to dodge
# case-insensitive filesystem foot-guns on Windows / macOS.
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Minimum context size we'll accept; smaller is almost certainly a
# misconfiguration that will produce empty responses.
_MIN_CTX_SIZE = 512

_PROVIDER_PREFIX = "llamafile://"


# --------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------- #

class RegistryError(Exception):
    """Base for all registry errors. Subclassed for the cases the CLI
    surfaces verbatim to the user."""


class LabelCollision(RegistryError):
    """A label is already registered."""


class PortCollision(RegistryError):
    """A port is already in use by another registry entry."""


class UnknownLabel(RegistryError):
    """``get`` / ``remove`` / ``rename`` against a label that isn't
    in the registry."""


class InvalidLabel(RegistryError):
    """Label doesn't match ``_LABEL_RE``."""


class InvalidGguf(RegistryError):
    """The path doesn't exist, isn't a file, or doesn't start with
    GGUF magic."""


class NoFreePort(RegistryError):
    """Auto-allocation couldn't find a free port in the configured
    range."""


# --------------------------------------------------------------------- #
# ModelSpec
# --------------------------------------------------------------------- #

@dataclass
class ModelSpec:
    """One row of the registry. Validated on construction (charset,
    ctx_size floor, mode whitelist); GGUF existence + magic is
    re-checked at ensure-time by the manager because users move
    GGUFs around."""

    label: str
    gguf_path: str
    ctx_size: int = 16384
    port: int = 0  # 0 means "allocate when added"
    mode: str = DEFAULT_MODE
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    label_aliases: list[str] = field(default_factory=list)
    added_at: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not _LABEL_RE.match(self.label):
            raise InvalidLabel(
                f"label {self.label!r} must match {_LABEL_RE.pattern} "
                "(lowercase, ASCII letters/digits/dots/dashes/underscores, "
                "1-64 chars, must start with a letter or digit)"
            )
        if self.ctx_size < _MIN_CTX_SIZE:
            raise RegistryError(
                f"ctx_size {self.ctx_size} below minimum {_MIN_CTX_SIZE}"
            )
        if self.mode not in ("auto", "cpu"):
            raise RegistryError(
                f"mode must be 'auto' or 'cpu', got {self.mode!r}"
            )
        if self.idle_timeout_seconds < 30:
            raise RegistryError(
                f"idle_timeout_seconds {self.idle_timeout_seconds} too low; "
                "minimum 30 (a reaper interval ago)"
            )

    def to_json(self) -> dict:
        """Serialisable form (without ``label`` — that's the dict key
        in the registry file)."""
        d = asdict(self)
        d.pop("label")
        return d

    @classmethod
    def from_json(cls, label: str, data: dict) -> "ModelSpec":
        """Lenient parser — missing optional fields fall back to
        defaults. Unknown fields are ignored (forward-compat)."""
        return cls(
            label=label,
            gguf_path=str(data.get("gguf_path", "")),
            ctx_size=int(data.get("ctx_size", 16384)),
            port=int(data.get("port", 0)),
            mode=str(data.get("mode", DEFAULT_MODE)),
            idle_timeout_seconds=int(
                data.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
            ),
            label_aliases=list(data.get("label_aliases", [])),
            added_at=str(data.get("added_at", "")),
            notes=str(data.get("notes", "")),
        )


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _gguf_magic_ok(path: str) -> bool:
    """Return True if ``path`` starts with the four bytes ``GGUF``.

    Re-used from install.py logic; inlined here so the registry has
    no install-time dependency. Returns False on any I/O error rather
    than raising — callers raise :class:`InvalidGguf` with a useful
    message.
    """
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_envelope() -> dict:
    """Fresh empty-registry envelope. Used on first load."""
    return {
        "schema_version": SCHEMA_VERSION,
        "max_concurrent_loaded": DEFAULT_MAX_CONCURRENT_LOADED,
        "default_port_range": list(DEFAULT_PORT_RANGE),
        "default_idle_timeout_seconds": DEFAULT_IDLE_TIMEOUT_SECONDS,
        "default_mode": DEFAULT_MODE,
        "models": {},
    }


def _migrate(data: dict) -> dict:
    """Schema migrations. Currently only one version, so this is a
    no-op except for upgrading defaults that may be missing on files
    written by an older CLI build."""
    if not isinstance(data, dict):
        return _default_envelope()
    sv = data.get("schema_version")
    if sv is None:
        data["schema_version"] = SCHEMA_VERSION
    # Backfill any top-level defaults that are missing — older files
    # may not have written them.
    env = _default_envelope()
    for key in ("max_concurrent_loaded", "default_port_range",
                "default_idle_timeout_seconds", "default_mode"):
        data.setdefault(key, env[key])
    data.setdefault("models", {})
    if not isinstance(data["models"], dict):
        data["models"] = {}
    return data


# --------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------- #

class Registry:
    """Pure I/O over ``~/.claude/llamafile-models.json``.

    Each public method reads-mutates-writes the file as one atomic
    operation. Concurrent CLI invocations may race (last-writer
    wins), but the file is never left half-written because
    ``_save`` writes to ``<path>.tmp`` and ``os.replace``s into
    place.
    """

    def __init__(self, path: Path = DEFAULT_REGISTRY_PATH):
        self.path = Path(path)

    # -------- load / save --------

    def load(self) -> dict:
        """Return the parsed envelope (with defaults backfilled).
        Returns an empty-registry envelope when the file doesn't
        exist or is corrupt; corrupt files are logged but not
        deleted (an operator can inspect them)."""
        if not self.path.exists():
            return _default_envelope()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "chat-model registry %s unreadable: %s — using empty",
                self.path, exc,
            )
            return _default_envelope()
        return _migrate(data)

    def save(self, data: dict) -> None:
        """Atomic write. ``mkdir -p`` the parent if missing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, self.path)

    def mtime(self) -> float:
        """Return the registry file's mtime, or 0.0 if missing.
        Used by ChatModelManager to detect external CLI mutations."""
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    # -------- read --------

    def list_labels(self) -> list[str]:
        return sorted(self.load().get("models", {}).keys())

    def list_specs(self) -> list[ModelSpec]:
        models = self.load().get("models", {})
        return [
            ModelSpec.from_json(label, data)
            for label, data in sorted(models.items())
        ]

    def get(self, label: str) -> ModelSpec:
        models = self.load().get("models", {})
        if label not in models:
            raise UnknownLabel(f"no chat model registered as {label!r}")
        return ModelSpec.from_json(label, models[label])

    def exists(self, label: str) -> bool:
        return label in self.load().get("models", {})

    def envelope_settings(self) -> dict:
        """Return the top-level (non-model) settings — concurrency
        cap, port range, defaults. Used by ChatModelManager."""
        data = self.load()
        return {
            "max_concurrent_loaded": int(data["max_concurrent_loaded"]),
            "default_port_range": tuple(data["default_port_range"]),
            "default_idle_timeout_seconds": int(
                data["default_idle_timeout_seconds"]
            ),
            "default_mode": str(data["default_mode"]),
        }

    # -------- mutations --------

    def add(
        self,
        label: str,
        gguf_path: str,
        *,
        ctx_size: int = 16384,
        port: Optional[int] = None,
        mode: str = DEFAULT_MODE,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
        notes: str = "",
    ) -> ModelSpec:
        """Register a new model. Refuses on label collision or
        port collision. ``port=None`` auto-allocates from the
        configured range."""
        gguf_abs = str(Path(gguf_path).expanduser().resolve())
        if not Path(gguf_abs).is_file():
            raise InvalidGguf(f"not a file: {gguf_abs}")
        if not _gguf_magic_ok(gguf_abs):
            raise InvalidGguf(
                f"{gguf_abs} does not start with GGUF magic — "
                "is this really a GGUF file?"
            )
        data = self.load()
        if label in data["models"]:
            raise LabelCollision(
                f"label {label!r} already registered; pick another or "
                f"`claude-hooks-models remove {label}` first"
            )

        used_ports = {
            int(spec.get("port", 0))
            for spec in data["models"].values()
            if spec.get("port")
        }

        if port is None:
            allocated = self._allocate_port(used_ports, data)
            port = allocated
        else:
            if port in used_ports:
                raise PortCollision(
                    f"port {port} already used by another registered "
                    "chat model"
                )

        spec = ModelSpec(
            label=label,
            gguf_path=gguf_abs,
            ctx_size=ctx_size,
            port=port,
            mode=mode,
            idle_timeout_seconds=idle_timeout_seconds,
            added_at=_now_iso(),
            notes=notes,
        )
        data["models"][label] = spec.to_json()
        self.save(data)
        return spec

    def remove(self, label: str) -> bool:
        data = self.load()
        if label not in data["models"]:
            return False
        del data["models"][label]
        self.save(data)
        return True

    def rename(self, old: str, new: str) -> ModelSpec:
        if not _LABEL_RE.match(new):
            raise InvalidLabel(
                f"new label {new!r} must match {_LABEL_RE.pattern}"
            )
        data = self.load()
        if old not in data["models"]:
            raise UnknownLabel(f"no chat model registered as {old!r}")
        if new in data["models"]:
            raise LabelCollision(f"label {new!r} already registered")
        data["models"][new] = data["models"].pop(old)
        self.save(data)
        return ModelSpec.from_json(new, data["models"][new])

    def copy(
        self,
        src: str,
        new_label: str,
        *,
        port: Optional[int] = None,
        ctx_size: Optional[int] = None,
        notes: str = "",
    ) -> ModelSpec:
        """Create a new entry pointing at the same GGUF as ``src``.
        Useful for ``same model, different ctx`` or aliasing.
        Always allocates a fresh port (the source's port is already
        bound to its label)."""
        if not _LABEL_RE.match(new_label):
            raise InvalidLabel(
                f"new label {new_label!r} must match {_LABEL_RE.pattern}"
            )
        data = self.load()
        if src not in data["models"]:
            raise UnknownLabel(f"no chat model registered as {src!r}")
        if new_label in data["models"]:
            raise LabelCollision(f"label {new_label!r} already registered")
        src_spec = ModelSpec.from_json(src, data["models"][src])

        used_ports = {
            int(s.get("port", 0))
            for s in data["models"].values()
            if s.get("port")
        }
        if port is None:
            port = self._allocate_port(used_ports, data)
        elif port in used_ports:
            raise PortCollision(
                f"port {port} already used by another registered "
                "chat model"
            )

        spec = ModelSpec(
            label=new_label,
            gguf_path=src_spec.gguf_path,
            ctx_size=ctx_size if ctx_size is not None else src_spec.ctx_size,
            port=port,
            mode=src_spec.mode,
            idle_timeout_seconds=src_spec.idle_timeout_seconds,
            added_at=_now_iso(),
            notes=notes or f"copy of {src}",
        )
        data["models"][new_label] = spec.to_json()
        self.save(data)
        return spec

    # -------- prefix / ref helpers --------

    @staticmethod
    def parse_ref(ref: str) -> tuple[str, str]:
        """Parse a model identifier. Returns ``(backend, target)``.

        Examples:
          ``"llamafile://gemma"`` -> ``("llamafile", "gemma")``
          ``"qwen:cloud"``        -> ``("ollama", "qwen:cloud")``
          ``"gemma"``             -> ``("ollama", "gemma")``

        The Ollama path is intentionally permissive — any string not
        starting with a known provider prefix is passed verbatim to
        Ollama, which interprets its own suffix conventions
        (``:cloud``, ``:latest``, etc.).
        """
        if ref.startswith(_PROVIDER_PREFIX):
            return ("llamafile", ref[len(_PROVIDER_PREFIX):])
        return ("ollama", ref)

    def normalize_ref(self, ref: str) -> str:
        """Strip the ``llamafile://`` prefix and validate the label
        exists. For Ollama refs, return unchanged. Raises
        :class:`UnknownLabel` for llamafile refs not in the registry."""
        backend, target = self.parse_ref(ref)
        if backend == "llamafile":
            if not self.exists(target):
                raise UnknownLabel(
                    f"no chat model registered as {target!r} "
                    f"(referenced as {ref!r}). "
                    "Add one with `claude-hooks-models add`."
                )
        return target

    # -------- internal --------

    def _allocate_port(self, used_ports: set, data: dict) -> int:
        """Pick the lowest free port in the configured range."""
        lo, hi = data["default_port_range"]
        for p in range(int(lo), int(hi) + 1):
            if p not in used_ports:
                return p
        raise NoFreePort(
            f"no free port in range [{lo}, {hi}]; "
            "free one with `claude-hooks-models remove <label>` or "
            "widen the range in the registry file"
        )
