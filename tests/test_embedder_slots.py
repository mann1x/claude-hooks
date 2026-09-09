"""Embedder slot count — it must survive config round-trips.

This setting has been lost twice, both times the same way: it was raised
on the running llamafile by hand, the daemon rebuilt the argv on the next
respawn, and the embedder silently went back to one slot. Nothing failed;
it just got slow again, and "slow" does not page anyone.

So the tests here are not really about ``--parallel`` being spelled
correctly. They pin the three places the value can quietly become 1:
the dataclass default, the config reader, and the install.py block
rebuild (which drops every key it does not explicitly name).
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks import embedding_manager as em

REPO = Path(__file__).resolve().parent.parent


def _cfg(**overrides) -> em.EmbeddingConfig:
    base = dict(llamafile_path="/path/to/llamafile", port=38199,
                ctx_size=16384, pooling="last", mode="cpu")
    base.update(overrides)
    return em.EmbeddingConfig(**base)


def _cmd(tmp_path, **overrides) -> list[str]:
    binp = tmp_path / "llamafile"
    binp.write_bytes(b"#!/bin/true\n")
    m = em.EmbeddingManager(_cfg(llamafile_path=str(binp), **overrides))
    return m._build_cmd()


class TestDefault:
    def test_default_is_three_slots(self):
        """The default itself, not just the shipped config: a wiped or
        hand-trimmed config must not yield a single-slot embedder."""
        assert em.EmbeddingConfig(llamafile_path="x").n_parallel == 3

    def test_spawn_passes_parallel(self, tmp_path):
        cmd = _cmd(tmp_path)
        assert "--parallel" in cmd
        assert cmd[cmd.index("--parallel") + 1] == "3"


class TestContextScaling:
    """llama.cpp divides --ctx-size across slots. If the total were
    passed unscaled, raising the slot count would silently shrink every
    embed's window — trading queueing for truncated vectors, which is
    strictly worse because it is invisible."""

    def test_total_ctx_is_scaled_by_slots(self, tmp_path):
        cmd = _cmd(tmp_path, ctx_size=16384, n_parallel=3)
        assert cmd[cmd.index("--ctx-size") + 1] == str(16384 * 3)

    def test_per_slot_window_is_preserved_across_slot_counts(self, tmp_path):
        for slots in (1, 2, 3, 8):
            cmd = _cmd(tmp_path, ctx_size=16384, n_parallel=slots)
            total = int(cmd[cmd.index("--ctx-size") + 1])
            assert total // slots == 16384

    def test_single_slot_matches_the_pre_change_command(self, tmp_path):
        cmd = _cmd(tmp_path, ctx_size=16384, n_parallel=1)
        assert cmd[cmd.index("--ctx-size") + 1] == "16384"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_degenerate_slot_counts_floor_at_one(self, tmp_path, bad):
        """Never emit ``--parallel 0``: llama.cpp would refuse to start
        and the embedder would be down rather than merely slow."""
        cmd = _cmd(tmp_path, n_parallel=bad)
        assert cmd[cmd.index("--parallel") + 1] == "1"
        assert cmd[cmd.index("--ctx-size") + 1] == "16384"


class TestConfigReader:
    def test_reads_explicit_value(self):
        cfg = em.config_from_dict({"embedding": {"n_parallel": 5}})
        assert cfg.n_parallel == 5

    def test_missing_key_defaults_to_three(self):
        """A config written by an install.py older than this change has
        no such key. It must not read as one slot."""
        cfg = em.config_from_dict({"embedding": {"port": 38092}})
        assert cfg.n_parallel == 3

    def test_zero_defaults_to_three(self):
        cfg = em.config_from_dict({"embedding": {"n_parallel": 0}})
        assert cfg.n_parallel == 3


class TestInstallerPreservation:
    """install.py rebuilds ``cfg["embedding"]`` from scratch on every
    run and keeps only the keys it names. That is the exact mechanism
    that dropped this setting before."""

    def test_existing_value_survives_a_reinstall(self):
        import install

        cfg = {"embedding": {"n_parallel": 6, "port": 38092}}
        with patch.object(Path, "is_file", return_value=True):
            block = install._setup_llamafile_engine(
                cfg, non_interactive=True, dry_run=False,
            )
        assert block["n_parallel"] == 6

    def test_absent_value_installs_as_three(self):
        import install

        with patch.object(Path, "is_file", return_value=True):
            block = install._setup_llamafile_engine(
                {}, non_interactive=True, dry_run=False,
            )
        assert block["n_parallel"] == 3


class TestShippedConfigs:
    """The committed example is what a new host starts from; the live
    config is what this host actually runs."""

    @pytest.mark.parametrize("name", ["claude-hooks.example.json",
                                      "claude-hooks.json"])
    def test_config_declares_three_slots(self, name):
        path = REPO / "config" / name
        if not path.exists():          # live config is gitignored
            pytest.skip(f"{name} not present")
        emb = json.loads(path.read_text()).get("embedding")
        if emb is None:
            pytest.skip(f"{name} has no embedding block")
        assert emb.get("n_parallel") == 3
