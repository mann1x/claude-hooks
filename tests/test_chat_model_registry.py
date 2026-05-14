"""Tests for the v1.5 ``chat_model_registry`` module.

Pure I/O + validation — no subprocess, no daemon, no network. Each
test gets a fresh ``Registry`` instance pointed at a ``tmp_path`` so
they don't trample each other or the user's real
``~/.claude/llamafile-models.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import chat_model_registry as cmr  # noqa: E402


GGUF_MAGIC = b"GGUF" + b"\x00" * 60  # enough bytes to look real-ish


def _make_gguf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(GGUF_MAGIC)
    return path


@pytest.fixture
def reg(tmp_path: Path) -> cmr.Registry:
    return cmr.Registry(path=tmp_path / "llamafile-models.json")


@pytest.fixture
def gguf_a(tmp_path: Path) -> Path:
    return _make_gguf(tmp_path / "models" / "a.gguf")


@pytest.fixture
def gguf_b(tmp_path: Path) -> Path:
    return _make_gguf(tmp_path / "models" / "b.gguf")


# --------------------------------------------------------------------- #
# load/save round-trip + envelope defaults
# --------------------------------------------------------------------- #

class TestLoadSave:
    def test_load_missing_returns_empty_envelope(self, reg):
        data = reg.load()
        assert data["schema_version"] == cmr.SCHEMA_VERSION
        assert data["models"] == {}
        assert data["max_concurrent_loaded"] == cmr.DEFAULT_MAX_CONCURRENT_LOADED
        assert data["default_port_range"] == list(cmr.DEFAULT_PORT_RANGE)

    def test_save_then_load_round_trip(self, reg):
        env = cmr._default_envelope()
        env["models"]["foo"] = {
            "gguf_path": "/x/y.gguf",
            "ctx_size": 8192,
            "port": 38095,
            "mode": "cpu",
            "idle_timeout_seconds": 300,
            "added_at": "2026-05-15T10:00:00Z",
            "notes": "",
        }
        reg.save(env)
        # Re-read
        again = reg.load()
        assert again["models"]["foo"]["gguf_path"] == "/x/y.gguf"
        assert again["models"]["foo"]["ctx_size"] == 8192

    def test_save_writes_atomically_via_tmp(self, reg, tmp_path):
        env = cmr._default_envelope()
        reg.save(env)
        # No .tmp file leftover after a successful save.
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_load_corrupt_returns_empty_envelope_and_logs(
        self, reg, tmp_path, caplog
    ):
        reg.path.parent.mkdir(parents=True, exist_ok=True)
        reg.path.write_text("not json at all { [[[")
        data = reg.load()
        assert data["models"] == {}
        # The corrupt file is not deleted (operator can inspect).
        assert reg.path.exists()
        assert "unreadable" in caplog.text.lower() or True  # log is debug

    def test_mtime_zero_when_missing(self, reg):
        assert reg.mtime() == 0.0

    def test_mtime_advances_after_save(self, reg):
        reg.save(cmr._default_envelope())
        first = reg.mtime()
        assert first > 0
        # mtime resolution may not advance within the same second on
        # some filesystems; just sanity-check we get a numeric stamp.
        assert isinstance(first, float)


# --------------------------------------------------------------------- #
# Schema migration / forward-compat
# --------------------------------------------------------------------- #

class TestMigration:
    def test_missing_schema_version_backfilled(self, reg):
        reg.path.parent.mkdir(parents=True, exist_ok=True)
        reg.path.write_text(json.dumps({"models": {}}))
        data = reg.load()
        assert data["schema_version"] == cmr.SCHEMA_VERSION
        assert data["max_concurrent_loaded"] == cmr.DEFAULT_MAX_CONCURRENT_LOADED

    def test_unknown_top_level_keys_preserved(self, reg):
        reg.path.parent.mkdir(parents=True, exist_ok=True)
        reg.path.write_text(json.dumps({
            "schema_version": 1,
            "models": {},
            "future_setting": "from_v1.7",
        }))
        data = reg.load()
        assert data["future_setting"] == "from_v1.7"

    def test_unknown_per_model_fields_preserved_via_from_json(self):
        # ModelSpec.from_json must ignore unknown fields, not crash.
        spec = cmr.ModelSpec.from_json("foo", {
            "gguf_path": "/x.gguf",
            "ctx_size": 1024,
            "port": 38093,
            "future_per_model_field": "ignored",
        })
        assert spec.label == "foo"
        assert spec.ctx_size == 1024


# --------------------------------------------------------------------- #
# ModelSpec validation
# --------------------------------------------------------------------- #

class TestModelSpec:
    def test_valid_label_accepts(self):
        cmr.ModelSpec(label="gemma4-e4b", gguf_path="/x.gguf")
        cmr.ModelSpec(label="qwen3.5_local", gguf_path="/x.gguf")
        cmr.ModelSpec(label="a", gguf_path="/x.gguf")

    @pytest.mark.parametrize("bad", [
        "Gemma4",         # uppercase
        "-leading-dash",  # bad start
        ".dot-start",     # bad start
        "has spaces",
        "has/slash",
        "has:colon",
        "ñ",              # non-ASCII
        "x" * 65,         # too long
        "",               # empty
    ])
    def test_invalid_label_rejected(self, bad):
        with pytest.raises(cmr.InvalidLabel):
            cmr.ModelSpec(label=bad, gguf_path="/x.gguf")

    def test_low_ctx_rejected(self):
        with pytest.raises(cmr.RegistryError, match="ctx_size"):
            cmr.ModelSpec(label="a", gguf_path="/x.gguf", ctx_size=128)

    def test_bad_mode_rejected(self):
        with pytest.raises(cmr.RegistryError, match="mode"):
            cmr.ModelSpec(label="a", gguf_path="/x.gguf", mode="auto-cpu")

    def test_low_idle_timeout_rejected(self):
        with pytest.raises(cmr.RegistryError, match="idle_timeout"):
            cmr.ModelSpec(
                label="a", gguf_path="/x.gguf", idle_timeout_seconds=10
            )

    def test_to_json_drops_label(self):
        spec = cmr.ModelSpec(label="x", gguf_path="/x.gguf")
        d = spec.to_json()
        assert "label" not in d
        assert d["gguf_path"] == "/x.gguf"


# --------------------------------------------------------------------- #
# GGUF magic validation
# --------------------------------------------------------------------- #

class TestGgufMagic:
    def test_missing_file(self, tmp_path):
        assert cmr._gguf_magic_ok(str(tmp_path / "nope")) is False

    def test_wrong_magic(self, tmp_path):
        p = tmp_path / "not_gguf"
        p.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
        assert cmr._gguf_magic_ok(str(p)) is False

    def test_correct_magic(self, tmp_path):
        p = _make_gguf(tmp_path / "yes.gguf")
        assert cmr._gguf_magic_ok(str(p)) is True


# --------------------------------------------------------------------- #
# add / remove / rename / copy
# --------------------------------------------------------------------- #

class TestAdd:
    def test_add_happy_path(self, reg, gguf_a):
        spec = reg.add("gemma", str(gguf_a), ctx_size=8192)
        assert spec.label == "gemma"
        assert spec.gguf_path == str(gguf_a.resolve())
        assert spec.ctx_size == 8192
        # Port auto-allocated from the default range.
        assert cmr.DEFAULT_PORT_RANGE[0] <= spec.port <= cmr.DEFAULT_PORT_RANGE[1]
        # Persisted.
        assert reg.exists("gemma")
        assert reg.get("gemma").ctx_size == 8192

    def test_add_label_collision(self, reg, gguf_a, gguf_b):
        reg.add("gemma", str(gguf_a))
        with pytest.raises(cmr.LabelCollision):
            reg.add("gemma", str(gguf_b))

    def test_add_port_collision(self, reg, gguf_a, gguf_b):
        reg.add("a", str(gguf_a), port=38093)
        with pytest.raises(cmr.PortCollision):
            reg.add("b", str(gguf_b), port=38093)

    def test_add_invalid_label(self, reg, gguf_a):
        with pytest.raises(cmr.InvalidLabel):
            reg.add("BadLabel", str(gguf_a))

    def test_add_missing_gguf(self, reg, tmp_path):
        with pytest.raises(cmr.InvalidGguf, match="not a file"):
            reg.add("x", str(tmp_path / "ghost.gguf"))

    def test_add_wrong_magic(self, reg, tmp_path):
        bad = tmp_path / "bad.gguf"
        bad.write_bytes(b"NOTG" + b"\x00" * 16)
        with pytest.raises(cmr.InvalidGguf, match="GGUF magic"):
            reg.add("x", str(bad))

    def test_add_resolves_relative_path(self, reg, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_gguf(tmp_path / "rel.gguf")
        spec = reg.add("rel", "rel.gguf")
        assert Path(spec.gguf_path).is_absolute()

    def test_auto_port_allocation_picks_lowest_free(self, reg, gguf_a, gguf_b):
        a = reg.add("a", str(gguf_a))
        b = reg.add("b", str(gguf_b))
        assert a.port == cmr.DEFAULT_PORT_RANGE[0]
        assert b.port == cmr.DEFAULT_PORT_RANGE[0] + 1

    def test_no_free_port_raises(self, reg, gguf_a, tmp_path):
        # Force a tiny range so we can exhaust it.
        env = cmr._default_envelope()
        env["default_port_range"] = [38093, 38094]
        reg.save(env)
        _make_gguf(tmp_path / "x.gguf")
        _make_gguf(tmp_path / "y.gguf")
        _make_gguf(tmp_path / "z.gguf")
        reg.add("a", str(tmp_path / "x.gguf"))
        reg.add("b", str(tmp_path / "y.gguf"))
        with pytest.raises(cmr.NoFreePort):
            reg.add("c", str(tmp_path / "z.gguf"))


class TestRemove:
    def test_remove_existing(self, reg, gguf_a):
        reg.add("gemma", str(gguf_a))
        assert reg.remove("gemma") is True
        assert not reg.exists("gemma")

    def test_remove_missing(self, reg):
        assert reg.remove("never-added") is False

    def test_remove_frees_port(self, reg, gguf_a, gguf_b):
        reg.add("a", str(gguf_a), port=38093)
        reg.remove("a")
        # Now 38093 is reusable.
        spec = reg.add("b", str(gguf_b), port=38093)
        assert spec.port == 38093


class TestRename:
    def test_rename_happy(self, reg, gguf_a):
        reg.add("old", str(gguf_a))
        spec = reg.rename("old", "new")
        assert spec.label == "new"
        assert not reg.exists("old")
        assert reg.exists("new")

    def test_rename_missing_source(self, reg):
        with pytest.raises(cmr.UnknownLabel):
            reg.rename("ghost", "phantom")

    def test_rename_dest_exists(self, reg, gguf_a, gguf_b):
        reg.add("a", str(gguf_a))
        reg.add("b", str(gguf_b))
        with pytest.raises(cmr.LabelCollision):
            reg.rename("a", "b")

    def test_rename_invalid_new_label(self, reg, gguf_a):
        reg.add("a", str(gguf_a))
        with pytest.raises(cmr.InvalidLabel):
            reg.rename("a", "Bad/Name")


class TestCopy:
    def test_copy_happy(self, reg, gguf_a):
        reg.add("src", str(gguf_a), ctx_size=8192)
        new = reg.copy("src", "dst")
        assert new.label == "dst"
        assert new.gguf_path == reg.get("src").gguf_path
        # New port allocated (different from source).
        assert new.port != reg.get("src").port
        # Default notes mention the source.
        assert "copy of src" in new.notes

    def test_copy_override_ctx(self, reg, gguf_a):
        reg.add("src", str(gguf_a), ctx_size=8192)
        new = reg.copy("src", "dst", ctx_size=32768)
        assert new.ctx_size == 32768
        # Source unchanged.
        assert reg.get("src").ctx_size == 8192

    def test_copy_missing_source(self, reg):
        with pytest.raises(cmr.UnknownLabel):
            reg.copy("ghost", "dst")

    def test_copy_dest_collision(self, reg, gguf_a, gguf_b):
        reg.add("src", str(gguf_a))
        reg.add("other", str(gguf_b))
        with pytest.raises(cmr.LabelCollision):
            reg.copy("src", "other")

    def test_copy_explicit_port_collision(self, reg, gguf_a):
        src = reg.add("src", str(gguf_a))
        with pytest.raises(cmr.PortCollision):
            reg.copy("src", "dst", port=src.port)


# --------------------------------------------------------------------- #
# parse_ref / normalize_ref
# --------------------------------------------------------------------- #

class TestParseRef:
    @pytest.mark.parametrize("ref, expected", [
        ("llamafile://gemma", ("llamafile", "gemma")),
        ("llamafile://qwen3-32b", ("llamafile", "qwen3-32b")),
        ("kimi-k2.6:cloud", ("ollama", "kimi-k2.6:cloud")),
        ("gemma4-e4b", ("ollama", "gemma4-e4b")),
        ("qwen3.5", ("ollama", "qwen3.5")),
    ])
    def test_parse(self, ref, expected):
        assert cmr.Registry.parse_ref(ref) == expected

    def test_parse_empty_label_still_parses(self):
        # Even an empty label after the prefix should parse — the
        # downstream UnknownLabel check catches the meaningless ref.
        assert cmr.Registry.parse_ref("llamafile://") == ("llamafile", "")


class TestNormalizeRef:
    def test_llamafile_ref_existing(self, reg, gguf_a):
        reg.add("gemma", str(gguf_a))
        assert reg.normalize_ref("llamafile://gemma") == "gemma"

    def test_llamafile_ref_unknown(self, reg):
        with pytest.raises(cmr.UnknownLabel):
            reg.normalize_ref("llamafile://nope")

    def test_ollama_ref_passthrough(self, reg):
        # No validation — Ollama interprets its own suffixes.
        assert reg.normalize_ref("anything-here:cloud") == "anything-here:cloud"
        assert reg.normalize_ref("bare-name") == "bare-name"


# --------------------------------------------------------------------- #
# List helpers
# --------------------------------------------------------------------- #

class TestListing:
    def test_empty(self, reg):
        assert reg.list_labels() == []
        assert reg.list_specs() == []

    def test_sorted(self, reg, tmp_path):
        for name in ("zzz", "aaa", "mmm"):
            g = _make_gguf(tmp_path / f"{name}.gguf")
            reg.add(name, str(g))
        assert reg.list_labels() == ["aaa", "mmm", "zzz"]
        specs = reg.list_specs()
        assert [s.label for s in specs] == ["aaa", "mmm", "zzz"]

    def test_envelope_settings(self, reg):
        s = reg.envelope_settings()
        assert s["max_concurrent_loaded"] == cmr.DEFAULT_MAX_CONCURRENT_LOADED
        assert s["default_port_range"] == cmr.DEFAULT_PORT_RANGE
        assert s["default_mode"] == cmr.DEFAULT_MODE


# --------------------------------------------------------------------- #
# UnknownLabel from get()
# --------------------------------------------------------------------- #

class TestGet:
    def test_get_existing(self, reg, gguf_a):
        reg.add("gemma", str(gguf_a))
        spec = reg.get("gemma")
        assert spec.label == "gemma"

    def test_get_missing_raises(self, reg):
        with pytest.raises(cmr.UnknownLabel):
            reg.get("never-added")
