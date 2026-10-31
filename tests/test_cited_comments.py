"""Plan council-memory M3: the comment at every cited line reaches the
critic and meta-critic (four critics missed "unwired on purpose")."""
from __future__ import annotations

import json

from consultants.engine import cited_comments as cc
from tests.test_evidence_bar import _Seq

INDEX_TS = """export function createExecutors(opts) {
\tconst receipts = opts.receipts;
\t// The read ledger is NOT wired by default, deliberately. Suppressing a copy
\t// on the grounds that the model "already has it" is a claim compaction
\t// can falsify.
\tconst ledger = opts.fileRead?.readLedger;
\treturn { receipts, ledger };
}
"""
PY = '''import os


# memoised: the probe is slow on Windows
@cache
def probe(path):
    """Whether ``path`` is a file we may read.

    Symlinks are not followed.
    """
    return os.path.isfile(path)


def plain(x):
    return x + 1  # off by one on purpose: callers count from 1
'''


def _tree(tmp_path):
    (tmp_path / "src" / "executors").mkdir(parents=True)
    (tmp_path / "src" / "executors" / "index.ts").write_text(INDEX_TS)
    (tmp_path / "src" / "probe.py").write_text(PY)
    return tmp_path


class TestTheBlock:

    def test_the_comment_above_a_code_line(self):
        lines = INDEX_TS.split("\n")
        assert cc.comment_block(lines, 6) == (3, 6)

    def test_a_cited_comment_line_runs_to_the_comments_end(self):
        assert cc.comment_block(INDEX_TS.split("\n"), 3) == (3, 5)

    def test_decorators_are_skipped_and_the_docstring_is_kept(self):
        lines = PY.split("\n")
        n = lines.index("def probe(path):") + 1
        first, last = cc.comment_block(lines, n)
        assert lines[first - 1].startswith("# memoised")
        assert lines[first].startswith("@cache")
        assert lines[last - 1].strip() == '"""'

    def test_a_trailing_comment_counts(self):
        lines = PY.split("\n")
        n = next(i for i, ln in enumerate(lines, 1) if "off by one" in ln)
        assert cc.comment_block(lines, n) == (n, n)

    def test_plain_code_has_none(self):
        lines = INDEX_TS.split("\n")
        assert cc.comment_block(lines, 2) is None
        assert cc.comment_block(lines, 99) is None


class TestFromReports:

    def test_cites_resolve_dedupe_and_count(self, tmp_path):
        root = str(_tree(tmp_path))
        reports = ["the ledger is unwired: src/executors/index.ts:6 — a gap",
                   "again src/executors/index.ts:6 and src/executors/index.ts:4",
                   "and the comment's last line src/executors/index.ts:5",
                   "src/executors/index.ts:2 has no comment; src/probe.py:6"]
        blocks = cc.blocks_for(reports, [root])
        assert [(b["path"], b["first"], b["last"], b["cites"]) for b in blocks] == [
            ("src/executors/index.ts", 3, 6, 2),
            ("src/executors/index.ts", 3, 5, 2),
            ("src/probe.py", 4, 10, 1)]
        assert "NOT wired by default, deliberately" in blocks[0]["text"]

    def test_a_short_path_resolves_like_the_linter(self, tmp_path):
        root = str(_tree(tmp_path))
        assert cc.blocks_for(["executors/index.ts:6"], [root])[0]["first"] == 3
        assert cc.blocks_for(["index.ts:6"], [root]) == []    # a bare name: no

    def test_whole_blocks_while_they_fit_then_named(self, tmp_path):
        root = str(_tree(tmp_path))
        blocks = cc.blocks_for(["src/executors/index.ts:6", "src/probe.py:6"], [root])
        one = cc.block_text(blocks, len(cc.HEADER) + 400)
        assert one.count("```") == 2 and "src/probe.py:6; read them" in one
        assert cc.block_text([], 10_000) == ""

    def test_no_comment_leaves_the_prompt_unchanged(self, tmp_path):
        root = str(_tree(tmp_path))
        msgs = [{"role": "user", "content": "x"}]
        assert cc.with_cited_comments(msgs, ["src/executors/index.ts:2"], [root],
                                      10_000) is msgs
        out = cc.with_cited_comments(msgs, ["src/executors/index.ts:6"], [root], 10_000)
        assert out[-1]["content"].startswith("x\n\n" + cc.HEADER)


def _state(root, **kw):
    return {"question": "q", "plan": "p", "cwd": root, "critique": "ok",
            "research": ["the read ledger is a gap: src/executors/index.ts:6"], **kw}


def test_the_critic_reads_the_comment(tmp_path):
    from consultants.engine.council import critic_node
    root = str(_tree(tmp_path))
    c = _Seq("DECISION: ready\nfine")
    critic_node(_state(root), chat_client=c, model="m", cwd=root)
    sent = json.dumps(c.calls[0])
    assert "CODE COMMENTS AT CITED LINES" in sent and "deliberately" in sent


def test_the_meta_critic_reads_the_comment(tmp_path):
    from consultants.engine import council
    from consultants.engine.storage import RoleTurn
    root = str(_tree(tmp_path))
    c = _Seq("DECISION: ready\nfine")
    council.meta_critic_node(
        _state(root, research_rounds_used=1, turns=[
            RoleTurn(role="critic", round=1, content="DECISION: ready\nok",
                     prompt_tokens=0, completion_tokens=0, duration_seconds=0.0)]),
        chat_client=c, model="m", cwd=root)
    assert "deliberately" in json.dumps(c.calls[0])


def test_a_critic_with_no_cwd_is_unchanged():
    from consultants.engine.council import critic_node
    c = _Seq("DECISION: ready\nfine")
    critic_node(_state(""), chat_client=c, model="m")
    assert "CODE COMMENTS AT CITED LINES" not in json.dumps(c.calls[0])
