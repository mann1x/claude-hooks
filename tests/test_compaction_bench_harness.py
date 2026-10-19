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


class _Rec:
    def __init__(self):
        self.events = []


def test_reads_are_never_refused_and_note_their_read_phase():
    rec = _Rec()
    ex = cb.ReadLog(_inner, rec)
    assert "content" in ex("read_file", '{"path": "src/net/backoff.py"}', ".")
    rec.events.append({"kind": "compaction"})
    assert "content" in ex("read_file", '{"path": "./src/a.py"}', ".")
    assert "content" in ex("read_file", '{"path": "src/net/backoff.py"}', ".")
    assert ex.phase == {"src/net/backoff.py": 0, "src/a.py": 1}
    ex("read_file", '{"path": "src/config/limits.py"}', ".")
    ex("read_file", '{"path": "src/config/limits.py"}', ".")
    assert ex.limits_reads == 2 and ex.limits_rereads_after_fold == 1


def test_the_table_is_graded_per_module_by_read_phase():
    purposes = {"src/a/lease_router.py": "evict",
                "src/b/shard_keeper.py": "reconcile",
                "src/c/span_loader.py": "flush"}
    phases = {"src/a/lease_router.py": 0, "src/b/shard_keeper.py": 1}
    answer = ("| src/a/lease_router.py | evicts stale leases |\n"
              "| src/b/shard_keeper.py | keeps shards |\n"
              "| src/c/span_loader.py | flushing spans |")
    assert cb.grade_table(answer, purposes, phases) == {
        "0": [1, 1], "1": [0, 1], "unread": [1, 1]}
    # A bare word in another module's row is not this module's row.
    assert cb.grade_table("| src/b/shard_keeper.py | evicts the lease_router |",
                          {"src/a/lease_router.py": "evict"},
                          {"src/a/lease_router.py": 0}) == {"0": [0, 1]}
    # The module's own name never counts as its purpose.
    assert cb.grade_table("| src/x/evict_router.py | routes |",
                          {"src/x/evict_router.py": "evict"},
                          {"src/x/evict_router.py": 0}) == {"0": [0, 1]}


def test_modules_say_what_they_do_and_names_never_give_it_away(tmp_path):
    order, purposes = cb.build_project(tmp_path, extra=60)
    assert "src/config/limits.py" not in purposes
    assert "src/net/backoff.py" not in purposes
    assert len({p.rsplit("/", 1)[-1] for p in order}) == len(order)
    for path, verb in purposes.items():
        text = (tmp_path / path).read_text()
        assert f"def {verb}_" in text  # the operation is in the code
        assert cb.verb_stem(verb) not in path.lower()
    # No word salad, no decoy echoing the bug.
    joined = " ".join(order)
    assert "window" not in joined and "retry" not in joined
