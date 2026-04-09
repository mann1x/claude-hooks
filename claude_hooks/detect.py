"""
Auto-detection of MCP servers from the user's Claude Code config.

The Claude Code config lives at:

- Linux/macOS:  ~/.claude.json
- Windows:      %USERPROFILE%\\.claude.json

Both files have a top-level ``mcpServers`` map and per-project ones under
``projects.<path>.mcpServers``. We scan everything and ask each provider
class to identify which entries look like its kind of backend.

The detection is two-phase:

1. **Name keyword match.** Each provider declares its name keywords
   (``qdrant``, ``memory``, ...). Any server whose key contains a keyword
   becomes a candidate.

2. **Tool probe.** If name matching is ambiguous (multiple matches) or
   yields nothing, we can fall back to actually probing each unmatched
   server's ``tools/list`` and looking for the provider's signature tool
   names. This is opt-in (slower, requires network) — see ``probe_unmatched``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from claude_hooks.providers import REGISTRY, Provider, ServerCandidate
from claude_hooks.providers.base import is_http_server, iter_mcp_servers


def claude_config_path() -> Path:
    """Return the path to ~/.claude.json on the current OS."""
    home = Path(os.path.expanduser("~"))
    return home / ".claude.json"


def claude_desktop_config_path() -> Optional[Path]:
    """Return the path to claude_desktop_config.json if it exists."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "Claude" / "claude_desktop_config.json"
            if p.exists():
                return p
    else:
        # macOS / linux
        for candidate in [
            Path("~/Library/Application Support/Claude/claude_desktop_config.json"),
            Path("~/.config/Claude/claude_desktop_config.json"),
        ]:
            p = candidate.expanduser()
            if p.exists():
                return p
    return None


def load_claude_config(path: Optional[Path] = None) -> dict:
    """Load and parse ~/.claude.json. Returns empty dict on missing/invalid file."""
    p = path or claude_config_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


@dataclass
class DetectionReport:
    """Result of running detect_all() across all providers."""

    by_provider: dict[str, list[ServerCandidate]]   # provider name → candidates
    all_http_servers: list[tuple[str, dict, str]]   # (key, cfg, source) for any HTTP server
    config_path: Path

    def candidates_for(self, provider_name: str) -> list[ServerCandidate]:
        return self.by_provider.get(provider_name, [])

    def unmatched_servers(self) -> list[tuple[str, dict, str]]:
        """HTTP servers that no provider name-matched."""
        matched_urls: set[str] = set()
        for cands in self.by_provider.values():
            for c in cands:
                matched_urls.add(c.url)
        return [
            (k, cfg, src)
            for (k, cfg, src) in self.all_http_servers
            if cfg.get("url") not in matched_urls
        ]


def detect_all(
    claude_config: Optional[dict] = None,
    *,
    config_path: Optional[Path] = None,
) -> DetectionReport:
    """Run name-based detection for every registered provider."""
    cfg = claude_config if claude_config is not None else load_claude_config(config_path)
    by_provider: dict[str, list[ServerCandidate]] = {}
    for cls in REGISTRY:
        by_provider[cls.name] = cls.detect(cfg)

    http_servers: list[tuple[str, dict, str]] = [
        (k, sc, src)
        for (k, sc, src) in iter_mcp_servers(cfg)
        if is_http_server(sc)
    ]
    return DetectionReport(
        by_provider=by_provider,
        all_http_servers=http_servers,
        config_path=config_path or claude_config_path(),
    )


def probe_unmatched(
    report: DetectionReport,
    *,
    timeout: float = 3.0,
) -> dict[str, list[ServerCandidate]]:
    """
    For each provider that has zero name-matched candidates, probe every
    unmatched HTTP server's ``tools/list`` and check whether its signature
    tools are present.

    Returns a dict of provider_name → newly-discovered candidates.
    """
    from claude_hooks.mcp_client import McpClient, McpError

    needs_probe: list[type[Provider]] = [
        cls for cls in REGISTRY if not report.by_provider.get(cls.name)
    ]
    if not needs_probe:
        return {}

    discovered: dict[str, list[ServerCandidate]] = {cls.name: [] for cls in needs_probe}
    for key, cfg, source in report.unmatched_servers():
        url = cfg["url"]
        client = McpClient(url, timeout=timeout, headers=cfg.get("headers") or {})
        try:
            tools = client.list_tools()
        except McpError:
            continue
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        for cls in needs_probe:
            if cls.signature_tools().issubset(names):
                discovered[cls.name].append(
                    ServerCandidate(
                        server_key=key,
                        url=url,
                        headers=cfg.get("headers") or {},
                        source=source,
                        confidence="tool_probe",
                        notes=f"tools/list matched signature {cls.signature_tools()}",
                    )
                )
    return discovered
