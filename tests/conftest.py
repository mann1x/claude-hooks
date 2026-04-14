"""
Shared pytest fixtures for the claude-hooks test suite.

Conventions:

- No fixture touches the real filesystem outside ``tmp_path`` / the
  ``tmp_claude_home`` helper.
- No fixture performs network I/O. Ollama / MCP / HTTP are mocked via
  helpers in :mod:`tests.mocks`.
- All fixtures are module-function-scoped so tests can mutate the
  results without affecting neighbours.

Smoke tests for each fixture live in ``tests/test_fixtures.py`` — if
those pass, downstream test files can rely on the fixtures' shape
without re-checking invariants.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import pytest

from claude_hooks.config import DEFAULT_CONFIG
from claude_hooks.providers.base import Memory, Provider, ServerCandidate


# --------------------------------------------------------------------- #
# Fake provider — minimal ``Provider`` subclass backed by an in-memory list.
# --------------------------------------------------------------------- #
class FakeProvider(Provider):
    """Minimal Provider for unit tests. Stores in RAM, no MCP calls."""

    name = "fake"
    display_name = "Fake"

    def __init__(
        self,
        *,
        name: str = "fake",
        recall_returns: Optional[list[Memory]] = None,
        store_errors: bool = False,
        recall_errors: bool = False,
    ):
        self.name = name
        self.display_name = name.capitalize()
        self.server = ServerCandidate(server_key=name, url="fake://", source="test")
        self.options: dict[str, Any] = {}
        self._recall_returns = recall_returns or []
        self._store_errors = store_errors
        self._recall_errors = recall_errors
        self.stored: list[tuple[str, dict]] = []
        self.recall_calls: list[tuple[str, int]] = []

    @classmethod
    def detect(cls, claude_config: dict) -> list[ServerCandidate]:
        return []

    @classmethod
    def signature_tools(cls) -> set[str]:
        return set()

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        self.recall_calls.append((query, k))
        if self._recall_errors:
            raise RuntimeError("simulated provider recall failure")
        return list(self._recall_returns)[:k]

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if self._store_errors:
            raise RuntimeError("simulated provider store failure")
        self.stored.append((content, dict(metadata or {})))


@pytest.fixture
def fake_provider():
    """Factory fixture — ``fake_provider(name="qdrant", recall_returns=[...])``."""
    def _make(**kwargs) -> FakeProvider:
        return FakeProvider(**kwargs)
    return _make


# --------------------------------------------------------------------- #
# base_config — mutable copy of DEFAULT_CONFIG with safe defaults for tests.
# --------------------------------------------------------------------- #
def _apply_overrides(cfg: dict, overrides: dict) -> dict:
    """Deep-merge ``overrides`` into ``cfg`` (in place). Returns cfg."""
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            _apply_overrides(cfg[k], v)
        else:
            cfg[k] = v
    return cfg


@pytest.fixture
def base_config():
    """Factory — ``base_config(hooks={"stop": {"enabled": False}})``."""
    def _make(**overrides) -> dict:
        cfg = deepcopy(DEFAULT_CONFIG)
        # Sane test defaults: don't write permission-scanner logs to $HOME.
        cfg["hooks"]["pre_tool_use"]["safety_log_enabled"] = False
        # Disable background reindex spawns in handler tests unless the
        # test asks for it explicitly.
        cfg["hooks"]["claudemem_reindex"]["enabled"] = False
        _apply_overrides(cfg, overrides)
        return cfg
    return _make


# --------------------------------------------------------------------- #
# Fake transcript — the JSONL shape stop.py reads via ``_read_transcript``.
# --------------------------------------------------------------------- #
def _msg(role: str, text: str = "", tool_uses: Optional[list[dict]] = None) -> dict:
    """Build one transcript-style message with ``message.content`` blocks."""
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        blocks.append({
            "type": "tool_use",
            "name": tu["name"],
            "input": tu.get("input", {}),
        })
    return {"message": {"role": role, "content": blocks}}


@pytest.fixture
def fake_transcript():
    """Factory for transcript lists.

    Usage::

        t = fake_transcript(
            user="do the thing",
            assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x.py"}}],
        )
    """
    def _make(
        *,
        user: str = "",
        assistant_text: str = "",
        assistant_tools: Optional[list[dict]] = None,
    ) -> list[dict]:
        out: list[dict] = []
        if user:
            out.append(_msg("user", user))
        if assistant_text or assistant_tools:
            out.append(_msg("assistant", assistant_text, assistant_tools))
        return out
    return _make


@pytest.fixture
def transcript_file(tmp_path, fake_transcript):
    """Write a fake transcript to tmp_path and return its str path.

    Usage: ``path = transcript_file(assistant_text="hi")``
    """
    def _make(**kw) -> str:
        path = tmp_path / "transcript.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for msg in fake_transcript(**kw):
                f.write(json.dumps(msg) + "\n")
        return str(path)
    return _make


# --------------------------------------------------------------------- #
# tmp_claude_home — redirects ``expand_user_path("~/...")`` under tmp.
# --------------------------------------------------------------------- #
@pytest.fixture
def tmp_claude_home(tmp_path, monkeypatch):
    """Force ``~`` expansion to a tmpdir so state files can't escape.

    Patches ``os.path.expanduser`` AND ``HOME`` so both ``expand_user_path``
    (used by config.py) and downstream library code land under tmp.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # expand_user_path uses os.path.expanduser; the HOME override above
    # covers Linux. On Windows os.path.expanduser uses %USERPROFILE%
    # which isn't relevant in this Linux-only CI.
    return fake_home
