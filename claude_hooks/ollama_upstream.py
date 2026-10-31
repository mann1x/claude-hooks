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
shims read it); ``--explain`` adds where it came from, and where the
Ollama Cloud key comes from (never the key).

Ollama Cloud used directly (``https://ollama.com``, no local Ollama) needs
a key (:func:`api_key`): the file named by ``ollama.api_key_file`` in
``config/claude-hooks.json``, else ``OLLAMA_API_KEY``. A service started
by systemd or the Task Scheduler never reads a shell profile, so the file
is the setting that reaches every engine. The key is sent only to
ollama.com itself, never to a LAN server.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
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


def _key_config() -> dict:
    """The config the key is read from: the host's (a seam tests close)."""
    return _host_config()


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


#: The environment variable Ollama's own clients read.
API_KEY_ENV = "OLLAMA_API_KEY"


def is_cloud(base_url: str) -> bool:
    """Whether ``base_url`` is Ollama Cloud itself (by host)."""
    from urllib.parse import urlsplit
    host = urlsplit(base_url or "").hostname or ""
    return host == "ollama.com" or host.endswith(".ollama.com")


def read_key_file(path: str) -> Optional[str]:
    """The first non-empty line of ``path`` (``~`` expanded), or ``None``."""
    try:
        text = Path(os.path.expanduser(path)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def api_key_source(cfg: Optional[dict] = None) -> tuple[Optional[str], str]:
    """``(key, source)`` for Ollama Cloud; ``source`` names the step that
    answered (``config ollama.api_key_file``, ``env OLLAMA_API_KEY``,
    ``none``), or why a configured file gave nothing."""
    cfg = _key_config() if cfg is None else cfg
    path = str(((cfg or {}).get("ollama") or {}).get("api_key_file") or "").strip()
    if path:
        key = read_key_file(path)
        if key:
            return key, "config ollama.api_key_file"
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env, f"env {API_KEY_ENV}"
    if path:
        return None, f"config ollama.api_key_file unreadable ({path})"
    return None, "none"


def api_key(base_url: Optional[str] = None,
            cfg: Optional[dict] = None) -> Optional[str]:
    """The Ollama Cloud key, only when ``base_url`` (default: :func:`url`)
    is ollama.com: a LAN server is never sent it."""
    target = url(cfg) if base_url is None else base_url
    if not is_cloud(target):
        return None
    return api_key_source(cfg)[0]


def auth_headers(base_url: str, cfg: Optional[dict] = None) -> dict:
    """``{"Authorization": "Bearer …"}`` for ollama.com with a key set,
    else ``{}``."""
    key = api_key(base_url, cfg)
    return {"Authorization": f"Bearer {key}"} if key else {}


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    u, source = resolve()
    if "--explain" not in argv:
        print(u)
        return 0
    print(f"{u}  ({source})")
    key, key_source = api_key_source()
    print(f"ollama cloud key: {'set' if key else 'not set'}  ({key_source})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
