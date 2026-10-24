"""The compaction bench's harness: project, sizing, reads, and grader v3 —
statuses read from the saved histories, the fix read from code only,
validity checks, regrade."""

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


def test_the_table_is_graded_per_module_by_its_status():
    purposes = {"src/a/lease_router.py": ("tier", "codec"),
                "src/b/shard_keeper.py": ("lineage", "weight"),
                "src/c/span_loader.py": ("tenant_id", "fence_token")}
    status = {"src/a/lease_router.py": "carried",
              "src/b/shard_keeper.py": "visible"}
    answer = ("| src/a/lease_router.py | tier, codec |\n"
              "| src/b/shard_keeper.py | lineage only |\n"
              "| src/c/span_loader.py | `tenant_id`, `fence_token` |")
    assert cb.grade_table(answer, purposes, status) == {
        "carried": [1, 1], "visible": [0, 1], "unseen": [1, 1]}
    # A bare word in another module's row is not this module's row.
    assert cb.grade_table("| src/b/shard_keeper.py | tier, codec; lease_router |",
                          {"src/a/lease_router.py": ("tier", "codec")},
                          {"src/a/lease_router.py": "visible"}) == {"visible": [0, 1]}


def test_a_row_naming_two_modules_counts_for_neither():
    purposes = {"src/a/queue.py": ("weight", "lineage"),
                "src/b/journal.py": ("weight", "lineage")}
    assert cb.grade_table("queue.py and journal.py | weight lineage",
                          purposes, {}) == {"unseen": [0, 2]}
    assert cb.grade_table("queue.py | weight lineage\njournal.py | weight, "
                          "lineage", purposes, {}) == {"unseen": [2, 2]}


# --- grader v2: status from what the model had in front of it -------------- #

P = {"src/a/lease_router.py": ("tier", "codec"),
     "src/b/shard_keeper.py": ("lineage", "weight"),
     "src/c/span_loader.py": ("tenant_id", "fence_token")}


def _read(i, path, body):
    return [{"role": "assistant", "content": "", "tool_calls": [
                {"id": f"r{i}", "type": "function", "function": {
                    "name": "read_file", "arguments": f'{{"path": "{path}"}}'}}]},
            {"role": "tool", "tool_call_id": f"r{i}", "content": body}]


def _grep(i, body):
    return [{"role": "assistant", "content": "", "tool_calls": [
                {"id": f"g{i}", "type": "function", "function": {
                    "name": "grep", "arguments": '{"pattern": "out"}'}}]},
            {"role": "tool", "tool_call_id": f"g{i}", "content": body}]


A_BODY = 'out["tier"] = x\nout["codec"] = y\nout["version"] = 1'
HEAD = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]


def test_each_raw_source_counts_and_the_marker_never_does():
    grep = ("/abs/project/src/b/shard_keeper.py:20:    out[\"lineage\"] = 1\n"
            "src/b/shard_keeper.py:21:    out[\"weight\"] = 2\n"
            "src/c/span_loader.py:20:    out[\"tenant_id\"] = 1")   # one field only
    msgs = (HEAD + _read(1, "./src/a/lease_router.py", A_BODY) + _grep(2, grep)
            + [{"role": "user", "_compaction_marker": True,
                "content": "span_loader.py writes tenant_id and fence_token"}])
    assert cb.evidence_in(msgs, P) == {"src/a/lease_router.py": "read",
                                       "src/b/shard_keeper.py": "tool"}
    own = HEAD + [{"role": "assistant",
                   "content": "| span_loader.py | tenant_id, fence_token |"}]
    assert cb.evidence_in(own, P) == {"src/c/span_loader.py": "text"}


def test_a_read_is_found_by_its_header_when_its_call_does_not_map():
    msgs = HEAD + [{"role": "tool", "tool_call_id": "lost", "content":
                    "src/a/lease_router.py:1-30  (file has 30 lines)\n"
                    "    20: out[\"tier\"] = x\n    21: out[\"codec\"] = y"}]
    assert cb.evidence_in(msgs, P) == {"src/a/lease_router.py": "read"}


def test_a_superseded_or_cut_read_is_not_raw():
    msgs = HEAD + _read(1, "src/a/lease_router.py",
                        "[outdated - see the latest file content]")
    assert cb.evidence_in(msgs, P) == {}


def test_carried_only_when_no_answering_call_has_it_raw():
    read = HEAD + _read(1, "src/a/lease_router.py", A_BODY)
    folded = HEAD + [{"role": "user", "_compaction_marker": True,
                      "content": "lease_router writes tier and codec"}]
    regrepped = folded + _grep(2, "src/a/lease_router.py:3:out[\"tier\"]\n"
                                  "src/a/lease_router.py:4:out[\"codec\"]")
    carried, _ = cb.status_from_history(
        [{"messages": read}, {"messages": folded, "answer": True}], P)
    assert carried == {"src/a/lease_router.py": "carried",
                       "src/b/shard_keeper.py": "unseen",
                       "src/c/span_loader.py": "unseen"}
    # Read, folded, then grepped again: the answer had it raw.
    vis, where = cb.status_from_history(
        [{"messages": read}, {"messages": regrepped, "answer": True}], P)
    assert vis["src/a/lease_router.py"] == "visible"
    assert where["src/a/lease_router.py"] == "tool"
    # Any answering call holding it raw makes it visible, not only the last.
    early, _ = cb.status_from_history(
        [{"messages": read, "answer": True}, {"messages": folded, "answer": True}],
        P)
    assert early["src/a/lease_router.py"] == "visible"


def _trial(arm="basic/system", hist="h", **table):
    return {"arm": arm, "trial": 0, "history": hist,
            "grader": cb.GRADER_VERSION, "table": table}


def test_validity_fails_a_leak_a_broken_control_and_a_missing_history():
    ok = _trial(carried=[20, 25], visible=[30, 30], unseen=[0, 40])
    assert all(v == "PASS" for v, _ in cb.validity([ok]))
    leak = _trial(carried=[20, 25], visible=[30, 30], unseen=[3, 40])
    broken = _trial(carried=[20, 25], visible=[10, 30])
    nohist = _trial(hist=None, carried=[20, 25], visible=[30, 30])
    old = {**ok, "grader": 1}
    for bad in (leak, broken, nohist, old):
        assert any(v == "FAIL" for v, _ in cb.validity([bad])), bad
    assert cb.validity([]) == [("FAIL", "the run has no trials")]
    thin = _trial(carried=[2, 3], visible=[30, 30])
    assert ("WARN", ) == tuple({v for v, _ in cb.validity([thin])} - {"PASS"})


def test_a_history_round_trips_and_regrades_to_the_same_table(tmp_path):
    calls = [{"role": "researcher", "messages": HEAD + _read(
                 1, "src/a/lease_router.py", A_BODY), "answer": False},
             {"role": "researcher:compaction", "messages": [], "answer": True},
             {"role": "researcher", "answer": True, "messages": HEAD + [
                 {"role": "user", "_compaction_marker": True, "content": "d"}]}]
    rel = cb.write_history(tmp_path, "full/system", 0, calls)
    assert rel == "histories/full-system-0.jsonl.gz"
    assert [c["role"] for c in cb.load_history(tmp_path, rel)] == [
        c["role"] for c in calls]
    trial = {"arm": "full/system", "trial": 0, "history": rel, "grader": 1,
             "answer": "| lease_router.py | tier, codec |",
             "status": {"src/a/lease_router.py": "folded"}}
    out = cb.regrade(trial, tmp_path, P)
    assert out["grader"] == cb.GRADER_VERSION
    assert out["status"]["src/a/lease_router.py"] == "carried"
    assert out["table"]["carried"] == [1, 1]
    assert cb.write_history(None, "a/b", 0, calls) is None


def test_a_lane_answering_from_one_grep_is_never_credited_as_carried(
        tmp_path):
    order, purposes = cb.build_project(tmp_path / "project", extra=20)
    t = cb.probe_trial(tmp_path / "project", order, purposes, tmp_path)
    assert t["error"] is None, t["error"]
    table = t["table"]
    # The probe worked: it got rows right from the grep alone ...
    assert table["visible"][0] >= 0.9 * len(purposes)
    # ... and none of them count as what a compaction carried.
    assert "carried" not in table and "unseen" not in table
    assert set(t["status_where"].values()) == {"tool"}
    assert t["history"] and (tmp_path / "preflight" / t["history"]).exists()
    assert cb.regrade(t, tmp_path / "preflight", purposes)["table"] == table
    checks = dict((w, v) for v, w in cb.validity([t]))
    assert any(v == "WARN" and "carried rows" in w for w, v in checks.items())
    assert cb.preflight(tmp_path / "project", order, purposes, tmp_path) == []


def test_the_preflight_refuses_a_grader_that_credits_the_probe(
        tmp_path, monkeypatch):
    order, purposes = cb.build_project(tmp_path / "project", extra=20)
    monkeypatch.setattr(cb, "status_from_history", lambda calls, p: (
        {k: "carried" for k in p}, {}))
    problems = cb.preflight(tmp_path / "project", order, purposes, tmp_path)
    assert any("carried" in x for x in problems)


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
                          {}) == {"unseen": [1, 1]}
    assert cb.answered_text([], "final") == "final"


def test_the_recorder_saves_each_call_as_it_was_sent():
    rec = cb.Recorder()
    msgs = [{"role": "user", "content": "q"}]
    rec.record_llm(role="researcher", round=0, lane_idx=0, model="m",
                   request={"messages": msgs, "tools": [
                       {"function": {"name": "grep"}}]},
                   response={"choices": [{"message": {"content": "a"}}]},
                   prompt_tokens=1, completion_tokens=1, duration_ms=1)
    msgs.append({"role": "tool", "content": "later"})   # the loop goes on
    saved = rec.history[0]
    assert saved["messages"] == [{"role": "user", "content": "q"}]
    assert saved["tools"] == ["grep"] and saved["answer"] is True


#: The diagnosis every answer shares: it states the unit, and the window in
#: milliseconds, in prose.
_DIAG = ("Root cause in `src/net/backoff.py` `should_retry`: `elapsed` is in "
         "milliseconds but `RETRY_WINDOW` is 450 deciseconds (= 45 seconds = "
         "45,000 ms). Ticket LDG-4471.\n\n")


def test_the_fix_is_graded_from_code_never_from_prose():
    order = ["src/net/backoff.py", "src/config/limits.py"]
    right = [  # the compaction-v7-g2 full answer, then each valid spelling
        "```python\nelapsed = (time.monotonic_ns() - started_ns) "
        "// 100_000_000\n```",
        "`return elapsed < limits.RETRY_WINDOW * 100`",
        "`return elapsed // 100 < limits.RETRY_WINDOW`",
        "`elapsed = (time.monotonic_ns() - started_ns) / 10**8`",
        "```\nRETRY_WINDOW = 45_000  # ms\n```",
        "```python\nreturn 100 * limits.RETRY_WINDOW > elapsed",  # unclosed
    ]
    wrong = [  # the compaction-v7-g2 basic and reduced answers, then prose
        "```python\nelapsed = (time.monotonic_ns() - started_ns) "
        "// 100_000\n```",
        "```python\n    return elapsed // 10 < limits.RETRY_WINDOW\n```",
        "Multiply RETRY_WINDOW * 100 or divide elapsed // 100.",
        "```python\nelapsed = (time.monotonic_ns() - started_ns) "
        "// 1_000_000\n```",
        "`RETRY_WINDOW = 45,000`",  # a tuple, not a number
    ]
    for fix in right:
        assert cb.grade(_DIAG + fix, order)["fixed"], fix
    for fix in wrong:
        g = cb.grade(_DIAG + fix, order)
        assert not g["conversion"] and not g["fixed"], fix
    assert not cb.grade(_DIAG, order)["fixed"]


def test_regrade_recomputes_the_fix_from_the_saved_answer(tmp_path):
    rel = cb.write_history(tmp_path, "basic/system", 0, [
        {"role": "researcher", "answer": True, "messages": HEAD}])
    wrong = _DIAG + "```python\nelapsed = (t1 - t0) // 100_000\n```"
    trial = {"arm": "basic/system", "trial": 0, "history": rel, "grader": 2,
             "answer": wrong, "fixed": True, "conversion": True, "status": {}}
    project = tmp_path / "project" / "src" / "net"
    project.mkdir(parents=True)
    (project / "backoff.py").write_text("")
    order = cb.saved_order(tmp_path)
    assert order == ["src/net/backoff.py"]
    out = cb.regrade(trial, tmp_path, P, order)
    assert out["fixed"] is False and out["conversion"] is False
    assert out["grader"] == cb.GRADER_VERSION
    # No order (nothing saved to rebuild it from): the fix is left as graded.
    assert cb.regrade(trial, tmp_path, P)["fixed"] is True


def test_a_digest_row_for_a_module_never_seen_is_counted_as_invented():
    """compaction-v7-g2 / -cite: every first fold, in both flows, gave
    fields for 66-118 modules the lane had not read, none right."""
    marker = {"role": "user", "_compaction_marker": True,
              "_compaction_generation": 1, "content": (
                  "- src/a/lease_router.py: tier, codec\n"          # was read
                  "- src/b/shard_keeper.py: tier, weight\n"          # invented
                  "- src/b/shard_keeper.py: tier, weight\n"          # again
                  "- src/c/span_loader.py: tenant_id, fence_token\n"  # invented
                  "- src/c/span_loader.py — still to read\n")}
    calls = [{"messages": HEAD + _read(1, "src/a/lease_router.py", A_BODY)},
             {"messages": HEAD + [marker]},
             {"messages": HEAD + [marker]}]               # same generation
    got = cb.invented_rows(calls, P)
    assert got == {"invented": 2, "right": 1,
                   "folds": [{"gen": 1, "invented": 2, "right": 1}]}
    # A module whose fields a grep had shown is not invented.
    grep = _grep(2, 'src/b/shard_keeper.py:3:out["lineage"] = 1\n'
                    'src/b/shard_keeper.py:4:out["weight"] = 2')
    calls[0]["messages"] = calls[0]["messages"] + grep
    assert cb.invented_rows(calls, P)["invented"] == 1
    assert cb.invented_rows([{"messages": HEAD}], P)["folds"] == []
    # Naming a module without giving it fields claims nothing.
    plan = {**marker, "content": "- src/c/span_loader.py — still to read"}
    assert cb.invented_rows([{"messages": HEAD + [plan]}], P)["invented"] == 0
