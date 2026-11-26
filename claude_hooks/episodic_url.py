"""Where this host reaches the episodic server.

One rule for every caller (``scripts/verify_deploy.py``, the SessionEnd
hook): on the server host the server listens on ``episodic.server_host`` /
``server_port`` (a wildcard bind is reached on loopback); a client uses
``episodic.server_url``. No address is shipped.
"""

from __future__ import annotations

from typing import Optional

DEFAULT_PORT = 11435


def server_base_url(ep_cfg: dict) -> Optional[str]:
    """The server's base URL without a trailing ``/``, or ``None`` when this
    host has no server to reach (episodic off, or a client with no URL)."""
    mode = (ep_cfg.get("mode") or "off").lower()
    if mode == "server":
        host = ep_cfg.get("server_host") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = int(ep_cfg.get("server_port") or DEFAULT_PORT)
        return f"http://{host}:{port}"
    if mode == "client" and ep_cfg.get("server_url"):
        return str(ep_cfg["server_url"]).rstrip("/")
    return None
