"""Plan H6: the decision-case miner's labels — struggle windows labelled
from the lane's own later calls, citation candidates sorted by where the
named symbol is."""
from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from benchmarks.consultants import mine_decision_cases as m


def _call(tool="read_file", args='{"path": "a.py"}', output="ok", error=None):
    return (tool, args, output, error)


def test_novelty_counts_repeats_failures_and_seen_output():
    calls = [_call(args='{"path": "a.py"}', output="A"),
             _call(args='{"path":"a.py"}', output="A2"),        # same call
             _call(args='{"path": "b.py"}', output="A"),        # seen output
             _call(args='{"path": "c.py"}', output="Error: no such file"),
             _call(args='{"path": "d.py"}', output="D", error="boom"),
             _call(args='{"path": "e.py"}', output='{"success": false}'),
             _call(args='{"path": "f.py"}', output="F")]
    assert m._novelty(calls) == [True, False, False, False, False, False, True]


def _db(tmp_path, calls):
    db = tmp_path / f"t{len(list(tmp_path.glob('t*.db')))}.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (ts REAL, kind TEXT, role TEXT, "
                "round INTEGER, lane_idx INTEGER, tool TEXT, args TEXT, "
                "output TEXT, error TEXT)")
    for i, (tool, args, output, error) in enumerate(calls):
        con.execute("INSERT INTO events VALUES (?, 'tool_call', 'researcher', "
                    "1, 0, ?, ?, ?, ?)", (float(i), tool, args, output, error))
    con.commit()
    return con


def _fresh(i):
    return _call(args=json.dumps({"path": f"f{i}.py"}), output=f"body {i}")


def _stale():
    return _call(args='{"path": "same.py"}', output="same")


def _labels(tmp_path, calls):
    con = _db(tmp_path, calls)
    try:
        cases = m.struggle_cases(con, {"sid": "csl-x"}, "find the bug")
    finally:
        con.close()
    return {c["origin"]["at_call"]: (c["candidate"], c["truth"]) for c in cases}


def test_a_lane_that_stays_stale_is_stuck(tmp_path):
    calls = [_fresh(0)] + [_stale() for _ in range(11)]
    assert _labels(tmp_path, calls)[6] == ("stuck", True)


def test_a_stale_window_the_lane_recovers_from_is_not_stuck(tmp_path):
    calls = [_fresh(0)] + [_stale() for _ in range(5)] + \
            [_fresh(i) for i in range(1, 7)]
    assert _labels(tmp_path, calls)[6] == ("recovered", False)


def test_progress_and_the_middle_ground(tmp_path):
    assert _labels(tmp_path, [_fresh(i) for i in range(12)])[6] == \
        ("progressing", False)
    # Half the next calls new: neither label — never guessed.
    mixed = [_fresh(i) for i in range(6)] + \
            [_fresh(6), _stale(), _fresh(7), _stale(), _fresh(8), _stale()]
    assert 6 not in _labels(tmp_path, mixed)


def test_the_window_is_rendered_as_the_live_seam_renders_it(tmp_path):
    con = _db(tmp_path, [_fresh(i) for i in range(12)])
    try:
        case = m.struggle_cases(con, {"sid": "csl-x"}, "goal")[0]
    finally:
        con.close()
    assert case["state"]["recent_calls"][0] == 'read_file {"path": "f0.py"} -> body 0'
    assert case["instructions"] == m.STRUGGLE_Q


@pytest.fixture
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    lines = ["def helper():", "    return 1", ""] + [f"# pad {i}" for i in range(40)] + \
            ["def target():", "    return helper()"]
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("\n".join(lines) + "\n")
    git("add", ".")
    git("commit", "-qm", "x")
    return tmp_path


def test_citation_candidates_by_where_the_symbol_is(repo):
    meta = {"sid": "csl-x", "cwd": str(repo), "started_at": 4_102_444_800}
    # The live linter only reads cites with a directory part.
    text = ("`target` calls the helper (`pkg/mod.py:44`). "
            "`target` is defined here (`pkg/mod.py:20`). "
            "`nowhere` is defined here (`pkg/mod.py:20`).")
    got = {c["origin"]["cite"]: c["candidate"]
           for c in m.citation_cases([text], meta)}
    # The second pkg/mod.py:20 cite is the same match and is taken once.
    assert got == {"pkg/mod.py:44": "names-it", "pkg/mod.py:20": "wrong-line"}
    case = m.citation_cases([text], meta)[0]
    assert case["truth"] is None and case["label_source"] == "pending"
    assert "44:     def target():" not in case["state"]["c0"]["code"]
    assert "44: def target():" in case["state"]["c0"]["code"]
