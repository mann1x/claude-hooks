"""Plan D5: read and grep say how much they returned (Cerebriline
``41059765c``, ``84ac149b1``)."""
from __future__ import annotations

import json

from claude_hooks.caliber_proxy import tools


def _read(tmp_path, **args):
    return tools.execute("read_file", json.dumps({"path": "f.txt", **args}),
                         str(tmp_path))


def test_cut_read_names_the_lines_shown_and_where_to_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_READ_MAX_BYTES", 100)
    (tmp_path / "f.txt").write_text("".join(f"line {i:03d} {'x' * 20}\n"
                                            for i in range(1, 51)))
    out = _read(tmp_path, start_line=10, end_line=40)
    header, *body = out.splitlines()
    # 30 bytes a line: three whole lines fit under 100.
    assert header.startswith("f.txt:10-12  (file has 50 lines)")
    assert "next: start_line=13" in header and "lines 13-40 are not shown" in header
    assert [b.split(":")[0].strip() for b in body] == ["10", "11", "12"]
    # Every line shown is whole: none was cut mid-line.
    assert all(b.endswith("x" * 20) for b in body)


def test_uncut_read_is_unchanged(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n")
    assert _read(tmp_path) == ("f.txt:1-3  (file has 3 lines)\n"
                               "     1: a\n     2: b\n     3: c")


def test_one_line_over_the_cap_shows_its_head_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_READ_MAX_BYTES", 50)
    (tmp_path / "f.txt").write_text("y" * 200 + "\nshort\n")
    header, line = _read(tmp_path).splitlines()
    assert header.startswith("f.txt:1-1")
    assert "line 1 is longer than ~48 KB; only its first 50 characters" in header
    assert "next: start_line=2" in header
    assert line == "     1: " + "y" * 50


def _grep(tmp_path, **args):
    return tools.execute("grep", json.dumps(args), str(tmp_path))


def test_grep_context_lines(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(
        ["one", "two", "HIT three", "four", "five", "six", "seven",
         "HIT eight", "HIT nine", "ten"]) + "\n")
    out = _grep(tmp_path, pattern="HIT", context_lines=1)
    assert out.splitlines() == [
        "a.py-2- two", "a.py:3: HIT three", "a.py-4- four", "--",
        "a.py-7- seven", "a.py:8: HIT eight", "a.py:9: HIT nine",
        "a.py-10- ten"]


def test_grep_without_context_is_unchanged(tmp_path):
    (tmp_path / "a.py").write_text("x\nHIT\ny\n")
    assert _grep(tmp_path, pattern="HIT") == "a.py:2: HIT"


def test_grep_stops_at_the_character_cap_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_GREP_MAX_CHARS", 200)
    (tmp_path / "a.py").write_text("".join(f"HIT {i} {'z' * 30}\n"
                                           for i in range(50)))
    out = _grep(tmp_path, pattern="HIT")
    assert len(out) < 400
    assert out.splitlines()[-1].startswith("... (output stopped at 200 characters")


def test_grep_match_cap_is_named_for_a_single_file_too(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_GREP_MAX_MATCHES", 3)
    (tmp_path / "a.py").write_text("HIT\n" * 10)
    out = _grep(tmp_path, pattern="HIT", path="a.py").splitlines()
    assert len(out) == 4 and out[-1] == "... (truncated at 3)"


def test_grep_schema_advertises_context_lines():
    spec = next(s for s in tools.openai_tool_specs()
                if s["function"]["name"] == "grep")
    assert spec["function"]["parameters"]["properties"]["context_lines"][
        "type"] == "integer"
