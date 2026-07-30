"""Tests for the 2026-07-25 embedder-timeout migration in install.py.

Background: embedding latency is superlinear in payload size. Measured
CPU-only on qwen3-embedding-0.6b (solidpc): 5 KB ~9 s, 16 KB ~48 s,
30 KB ~135 s. ``pgvector.store()`` issues *two* embeds (dedup recall +
content), so a 5 KB memory costs ~19 s — more than the 20 s Stop-hook
cap older installs wrote into ``settings.json``. The hook was SIGTERMed
before the embedder's own 30 s timeout could fire, the memory was
silently dropped, and the session reported the embedder as down.

New installs pick the corrected values up from ``DEFAULT_CONFIG`` and
the hook templates. Existing installs keep whatever their config
already holds — which for every pre-2026-07-25 host is exactly the
broken combination — hence ``_migrate_embedder_timeouts``.

The contract under test: raise a value only when it is still at-or-
below the *old* shipped default, so a deliberately-raised ceiling
survives a re-run untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _prefix_config() -> dict:
    """A pandorum-shaped pre-fix config: LAN llamafile, daemon_ensure off."""
    return {
        "providers": {
            "pgvector": {
                "timeout": 10.0,
                "embedder_options": {
                    "url": "http://192.168.178.2:38092/embedding",
                    "timeout": 30.0,
                    "daemon_ensure": False,
                },
            },
        },
        "embedding": {"idle_timeout_seconds": 300},
        "hooks": {"stop": {"detach_store": False}},
    }


class TestEmbedderTimeoutMigration:
    def test_raises_all_old_defaults(self):
        cfg = _prefix_config()
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        pg = cfg["providers"]["pgvector"]
        assert pg["embedder_options"]["timeout"] == 180.0
        assert pg["timeout"] == 30.0
        assert cfg["embedding"]["idle_timeout_seconds"] == 3600
        assert cfg["hooks"]["stop"]["detach_store"] is True

    def test_preserves_operator_raised_values(self):
        """A value the operator chose is never lowered or touched."""
        cfg = {
            "providers": {
                "pgvector": {
                    "timeout": 45.0,
                    "embedder_options": {"timeout": 240.0},
                },
            },
            "embedding": {"idle_timeout_seconds": 7200},
            "hooks": {"stop": {"detach_store": True}},
        }
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        pg = cfg["providers"]["pgvector"]
        assert pg["embedder_options"]["timeout"] == 240.0
        assert pg["timeout"] == 45.0
        assert cfg["embedding"]["idle_timeout_seconds"] == 7200
        assert cfg["hooks"]["stop"]["detach_store"] is True

    def test_idempotent(self):
        cfg = _prefix_config()
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        once = repr(cfg)
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        assert repr(cfg) == once

    def test_quiet_when_nothing_to_do(self, capsys):
        cfg = _prefix_config()
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        capsys.readouterr()
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        assert capsys.readouterr().out == ""

    def test_covers_sqlite_vec_too(self):
        cfg = {
            "providers": {
                "sqlite_vec": {
                    "timeout": 10.0,
                    "embedder_options": {"timeout": 30.0},
                },
            },
        }
        install._migrate_embedder_timeouts(cfg, dry_run=False)
        sv = cfg["providers"]["sqlite_vec"]
        assert sv["timeout"] == 30.0
        assert sv["embedder_options"]["timeout"] == 180.0

    def test_tolerates_missing_and_garbage_keys(self):
        """Never raise on a hand-edited or partial config."""
        install._migrate_embedder_timeouts({}, dry_run=False)
        install._migrate_embedder_timeouts(
            {"providers": {"pgvector": {
                "timeout": "not-a-number",
                "embedder_options": {"timeout": None},
            }}},
            dry_run=False,
        )
        install._migrate_embedder_timeouts(
            {"providers": None, "embedding": "nope", "hooks": {}},
            dry_run=False,
        )

    def test_dry_run_still_reports(self, capsys):
        """dry_run only annotates the output; the caller does not persist."""
        cfg = _prefix_config()
        install._migrate_embedder_timeouts(cfg, dry_run=True)
        assert "[dry-run]" in capsys.readouterr().out
