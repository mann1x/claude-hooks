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


def test_the_table_is_graded_per_module_by_what_the_answer_held():
    purposes = {"src/a/lease_router.py": ("tier", "codec"),
                "src/b/shard_keeper.py": ("lineage", "weight"),
                "src/c/span_loader.py": ("tenant_id", "fence_token")}
    status = {"src/a/lease_router.py": "folded",
              "src/b/shard_keeper.py": "kept"}
    answer = ("| src/a/lease_router.py | tier, codec |\n"
              "| src/b/shard_keeper.py | lineage only |\n"
              "| src/c/span_loader.py | `tenant_id`, `fence_token` |")
    assert cb.grade_table(answer, purposes, status) == {
        "folded": [1, 1], "kept": [0, 1], "unread": [1, 1]}
    # A bare word in another module's row is not this module's row.
    assert cb.grade_table("| src/b/shard_keeper.py | tier, codec; lease_router |",
                          {"src/a/lease_router.py": ("tier", "codec")},
                          {"src/a/lease_router.py": "kept"}) == {"kept": [0, 1]}


def test_a_row_naming_two_modules_counts_for_neither():
    purposes = {"src/a/queue.py": ("weight", "lineage"),
                "src/b/journal.py": ("weight", "lineage")}
    assert cb.grade_table("queue.py and journal.py | weight lineage",
                          purposes, {}) == {"unread": [0, 2]}
    assert cb.grade_table("queue.py | weight lineage\njournal.py | weight, "
                          "lineage", purposes, {}) == {"unread": [2, 2]}


def test_read_status_tells_folded_from_kept_and_grep_from_unread():
    read = {"src/a.py": 0, "src/b.py": 1}
    held = {"src/b.py"}
    got = {p: cb.read_status(p, read, held, {"src/c.py", "src/a.py"})
           for p in ("src/a.py", "src/b.py", "src/c.py", "src/d.py")}
    assert got == {"src/a.py": "folded", "src/b.py": "kept",
                   "src/c.py": "grep", "src/d.py": "unread"}


def test_verbatim_reads_are_the_read_results_still_in_the_history():
    def call(i, path):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {
                "name": "read_file", "arguments": f'{{"path": "{path}"}}'}}]}
    msgs = [{"role": "user", "content": "q"},
            {"role": "user", "content": "digest", "_compaction_marker": True},
            call(2, "./src/b.py"),
            {"role": "tool", "tool_call_id": "c2", "content": "body"},
            call(3, "src/c.py"),
            {"role": "tool", "tool_call_id": "c3", "content": "error: no"},
            {"role": "tool", "tool_call_id": "g1", "content": "src/e.py:1:x"},
            call(4, "src/d.py")]                 # answer not in yet
    assert cb.verbatim_reads(msgs) == {"src/b.py"}


def test_grep_results_note_the_modules_they_show():
    ex = cb.ReadLog(_inner, _Rec())
    ex("grep", '{"pattern": "x"}', ".")
    assert ex.grepped == {"src/a.py", "src/b.py"}
    assert ex.phase == {}


def test_the_main_operation_writes_exactly_two_fields(tmp_path):
    order, purposes = cb.build_project(tmp_path, extra=10)
    path, (f1, f2) = next(iter(purposes.items()))
    text = (tmp_path / path).read_text()
    assert "out.update" not in text
    body = text[text.index("def _"):text.index("return out")]
    written = set(__import__("re").findall(r'out\["(\w+)"\] =', body))
    assert written == {f1, f2, "version"}


def test_one_nudge_names_the_unread_modules_and_never_refuses():
    rec = _Rec()
    ex = cb.ReadLog(_inner, rec, order=["src/a.py", "src/b.py"])
    ex("read_file", '{"path": "src/a.py"}', ".")
    note = ex.report_check([])
    assert "1 of 2" in note and "src/b.py" in note
    ex("read_file", '{"path": "src/b.py"}', ".")
    assert ex.report_check([]) is None


def test_modules_say_what_they_write_and_names_never_give_it_away(tmp_path):
    order, purposes = cb.build_project(tmp_path, extra=60)
    assert "src/config/limits.py" not in purposes
    assert "src/net/backoff.py" not in purposes
    assert len({p.rsplit("/", 1)[-1] for p in order}) == len(order)
    for path, (f1, f2) in purposes.items():
        text = (tmp_path / path).read_text()
        assert f'out["{f1}"]' in text and f'out["{f2}"]' in text
        assert f1 not in path and f2 not in path
    joined = " ".join(order)
    assert "window" not in joined and "retry" not in joined


def test_an_answer_sent_back_by_the_nudge_is_still_graded():
    table = "| src/a/lease_router.py | tier, codec |"
    text = cb.answered_text([table], "My previous response is complete.")
    assert cb.grade_table(text, {"src/a/lease_router.py": ("tier", "codec")},
                          {}) == {"unread": [1, 1]}
    assert cb.answered_text([], "final") == "final"
