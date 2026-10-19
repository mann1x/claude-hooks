"""The compaction bench's harness (task v4): in-order reads, realistic
filler, sizing to the production window."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "benchmarks" / "consultants"))

import compaction_bench as cb  # noqa: E402


def _inner(name, args, cwd):
    if name == "grep":
        return ("src/a.py:1:x = 1\nsrc/b.py:2:RETRY_WINDOW\n"
                "src/b.py-3-context")
    return "content of " + str(args)


def test_reads_are_refused_past_the_next_module():
    ex = cb.InOrder(_inner, ["src/a.py", "src/b.py", "src/c.py"])
    out = ex("read_file", '{"path": "src/c.py"}', ".")
    assert out.startswith("error") and "src/a.py next" in out
    assert ex.refused == 1 and ex.next == 0
    assert "content" in ex("read_file", '{"path": "src/a.py"}', ".")
    assert ex.next == 1
    # A module already read may be read again; it does not advance.
    assert "content" in ex("read_file", '{"path": "./src/a.py"}', ".")
    assert ex.next == 1


def test_grep_shows_no_line_of_a_module_not_yet_reached():
    ex = cb.InOrder(_inner, ["src/a.py", "src/b.py"])
    assert "src/" not in ex("grep", '{"pattern": "x"}', "."
                            ).replace("(modules", "")
    ex("read_file", '{"path": "src/a.py"}', ".")
    out = ex("grep", '{"pattern": "x"}', ".")
    assert "src/a.py:1" in out and "src/b.py" not in out
    assert "not yet reached" in out
    ex("read_file", '{"path": "src/b.py"}', ".")
    assert "src/b.py:2" in ex("grep", '{"pattern": "x"}', ".")


def test_filler_is_named_like_the_project_and_interleaved():
    mods = cb.modules_for(127)
    assert len(mods) == len(set(mods)) == len(cb.MODULES) + 127
    assert mods[0] == "config/limits" and mods[-1] == "net/backoff"
    assert not any("part_" in m for m in mods)
    # The real modules are not left in one block.
    real = [i for i, m in enumerate(mods) if m in cb.MODULES]
    assert real[-2] - real[1] > len(cb.MODULES)


def test_the_project_grows_by_modules_never_by_module_size():
    funcs, extra, need = cb.scale_for(trigger=87_771, target=28_976, folds=3)
    assert funcs == cb.FUNCS
    assert need == int(1.25 * (87_771 + 2 * (87_771 - 28_976)))
    assert extra > 100
    small = cb.scale_for(trigger=21_942, target=6_857, folds=3)
    assert small[0] == cb.FUNCS and small[1] < extra


def test_a_report_before_every_module_is_sent_back_once_naming_the_next():
    ex = cb.InOrder(_inner, ["src/a.py", "src/b.py"])
    ex("read_file", '{"path": "src/a.py"}', ".")
    note = ex.report_check([])
    assert "1 of 2" in note and "src/b.py" in note
    ex("read_file", '{"path": "src/b.py"}', ".")
    assert ex.report_check([]) is None
