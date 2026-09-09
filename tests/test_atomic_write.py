"""Atomic writes under concurrency — claude_hooks/_atomic.py.

The bug this replaces was not corruption. Every writer produced a
complete document and ``os.replace`` was genuinely atomic; the flaw was
that all writers derived the *same* temp path from the destination, so
they clobbered each other's temp file and all but one lost the race with
``[Errno 2]``. Both call sites swallow OSError (they are best-effort
caches), so the update simply vanished.

The property under test is therefore "every concurrent writer succeeds",
not "the file is valid JSON" — the old code already passed the latter.
"""
import json
import threading
from pathlib import Path

import pytest

from claude_hooks import decay, hyde_cache
from claude_hooks._atomic import write_text_atomic


class TestBasics:
    def test_writes_content(self, tmp_path):
        p = tmp_path / "out.json"
        write_text_atomic(p, '{"a": 1}')
        assert json.loads(p.read_text()) == {"a": 1}

    def test_replaces_existing(self, tmp_path):
        p = tmp_path / "out.json"
        p.write_text("old")
        write_text_atomic(p, "new")
        assert p.read_text() == "new"

    def test_creates_parent_directory(self, tmp_path):
        p = tmp_path / "deep" / "nested" / "out.json"
        write_text_atomic(p, "x")
        assert p.read_text() == "x"

    def test_leaves_no_temp_files(self, tmp_path):
        p = tmp_path / "out.json"
        for i in range(5):
            write_text_atomic(p, str(i))
        assert [f.name for f in tmp_path.iterdir()] == ["out.json"]

    def test_temp_file_removed_when_write_fails(self, tmp_path):
        """A unique temp name means failed attempts leave unique litter,
        so cleanup matters more here than with a shared name."""
        p = tmp_path / "out.json"
        # Not a str -> f.write raises TypeError partway through, after
        # the temp file exists but before the replace.
        with pytest.raises(TypeError):
            write_text_atomic(p, 123)  # type: ignore[arg-type]
        assert list(tmp_path.iterdir()) == []

    def test_destination_intact_when_write_fails(self, tmp_path):
        """A failed update must not destroy the previous good file."""
        p = tmp_path / "out.json"
        p.write_text("previous")
        with pytest.raises(TypeError):
            write_text_atomic(p, 123)  # type: ignore[arg-type]
        assert p.read_text() == "previous"

    def test_unwritable_parent_raises_oserror(self, tmp_path):
        """Callers catch OSError to log-and-continue; the helper must
        keep raising it rather than inventing a new exception type."""
        p = tmp_path / "file_not_dir" / "out.json"
        (tmp_path / "file_not_dir").write_text("i am a file")
        with pytest.raises(OSError):
            write_text_atomic(p, "x")


def _hammer(fn, n=12):
    """Run ``fn(i)`` on n threads, collecting any exception."""
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def run(i):
        try:
            start.wait(timeout=5)
            fn(i)
        except BaseException as e:  # noqa: BLE001 - recorded, re-raised below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return errors


class TestConcurrency:
    def test_concurrent_writers_all_succeed(self, tmp_path):
        p = tmp_path / "shared.json"
        errors = _hammer(lambda i: write_text_atomic(p, json.dumps({"w": i})))
        assert errors == []
        json.loads(p.read_text())  # last writer wins, document intact

    def test_no_temp_litter_after_a_storm(self, tmp_path):
        p = tmp_path / "shared.json"
        _hammer(lambda i: write_text_atomic(p, json.dumps({"w": i})))
        assert [f.name for f in tmp_path.iterdir()] == ["shared.json"]


class TestCallSites:
    """The two loggers that were reporting the ENOENT in production."""

    def test_decay_history_survives_concurrent_saves(self, tmp_path, caplog):
        path = tmp_path / "claude-hooks-decay.json"
        with caplog.at_level("WARNING", logger="claude_hooks.decay"):
            errors = _hammer(
                lambda i: decay._save_history(path, {f"h{i}": {"recall_count": i}})
            )
        assert errors == []
        assert "failed to save decay history" not in caplog.text
        assert json.loads(path.read_text())["version"] == 1

    def test_hyde_cache_survives_concurrent_saves(self, tmp_path, caplog):
        path = tmp_path / "claude-hooks-hyde-cache.json"
        with caplog.at_level("DEBUG", logger="claude_hooks.hyde_cache"):
            errors = _hammer(
                lambda i: hyde_cache._save(path, {f"k{i}": {"expansion": "e",
                                                            "ts": 1.0}})
            )
        assert errors == []
        assert "hyde cache save failed" not in caplog.text

    def test_hyde_cache_roundtrip_still_works(self, tmp_path):
        path = tmp_path / "cache.json"
        hyde_cache.put("prompt", "model", "expansion", path=path, now=1000.0)
        assert hyde_cache.get("prompt", "model", path=path,
                              now=1000.0) == "expansion"

    def test_decay_roundtrip_still_works(self, tmp_path):
        path = tmp_path / "decay.json"
        decay._save_history(path, {"abc": {"last_recalled": "x",
                                           "recall_count": 2}})
        assert decay._load_history(path)["abc"]["recall_count"] == 2

    def test_save_errors_are_still_swallowed(self, tmp_path, caplog):
        """Best-effort semantics are preserved: a genuinely unwritable
        path logs and returns rather than killing the hook."""
        bad = tmp_path / "afile" / "decay.json"
        (tmp_path / "afile").write_text("not a dir")
        with caplog.at_level("WARNING", logger="claude_hooks.decay"):
            decay._save_history(bad, {"h": {}})
        assert "failed to save decay history" in caplog.text


class TestNoSharedTempPathRemains:
    def test_call_sites_no_longer_derive_a_shared_temp_name(self):
        """Guard against a regression that reintroduces the shared name
        by hand in either module."""
        for mod in (decay, hyde_cache):
            src = Path(mod.__file__).read_text()
            code = "\n".join(
                line for line in src.splitlines()
                if not line.lstrip().startswith("#")
            )
            assert 'with_suffix(path.suffix + ".tmp")' not in code
