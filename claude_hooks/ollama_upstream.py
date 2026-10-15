"""The Ollama server this host's engines talk to (plan §8).

One answer for the /consultants engine, /get-advice, the caliber
grounding proxy and the CLIs that query a model list or an account.
No host address is shipped: a host names its server, and a host that
names none gets Ollama's own default.

Order:

1. ``CALIBER_GROUNDING_UPSTREAM`` — the per-process override every
   component already honours (a trailing ``/v1`` is dropped);
2. ``OLLAMA_HOST`` — only when it is a URL (``http://`` / ``https://``);
   a bare bind address such as ``0.0.0.0`` names where a local Ollama
   listens, not where to reach one, and is ignored;
3. ``ollama.url`` in ``config/claude-hooks.json`` — the host's setting,
   written by ``install.py``;
4. :data:`DEFAULT_URL`.

``python -m claude_hooks.ollama_upstream`` prints the URL (the shell
shims read it); ``--explain`` adds where it came from.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

#: Ollama's own default listen address.
DEFAULT_URL = "http://localhost:11434"


def normalise(url: str) -> str:
    """A base URL without a trailing slash or OpenAI-compat ``/v1``."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")].rstrip("/")
    return u


def _host_config() -> dict:
    try:
        from claude_hooks.config import load_config
        return load_config()
    except Exception:  # pragma: no cover - a broken config is "unset"
        return {}


def resolve(cfg: Optional[dict] = None) -> tuple[str, str]:
    """``(url, source)``; ``source`` names the step of the order that
    answered, for ``--explain`` and ``verify_deploy``."""
    upstream = normalise(os.environ.get("CALIBER_GROUNDING_UPSTREAM", ""))
    if upstream:
        return upstream, "env CALIBER_GROUNDING_UPSTREAM"
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host.startswith(("http://", "https://")):
        return normalise(host), "env OLLAMA_HOST"
    cfg = _host_config() if cfg is None else cfg
    configured = normalise(str(((cfg or {}).get("ollama") or {}).get("url") or ""))
    if configured:
        return configured, "config ollama.url"
    return DEFAULT_URL, "default"


def url(cfg: Optional[dict] = None) -> str:
    return resolve(cfg)[0]


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    u, source = resolve()
    print(f"{u}  ({source})" if "--explain" in argv else u)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
