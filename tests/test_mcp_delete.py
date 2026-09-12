"""Memory deletion over MCP — the tool, its id plumbing, and its reach.

Before this, the memory stores were append-only from the outside: the
providers had ``delete_by_hashes`` but its only caller was the
consultants TTL reaper, so a wrong memory recalled by a user could be
read forever and removed by nothing.

Two properties matter more than the tool existing:

1. **Recall must hand back something delete can take.** ``Memory``
   carries no id, so unless recall surfaces ``content_hash`` the client
   has nothing to name. A delete tool without that is a tool you cannot
   aim.
2. **Delete must reach as far as recall does.** ``recall`` spans
   ``additional_tables``; the provider's ``delete_by_hashes`` defaults
   to the primary table only (what the TTL reaper means). If the MCP
   tool inherited that default, a user would watch a delete report
   success on a row that is still there.
"""
from claude_hooks.mcp_format import (
    format_delete_result,
    format_memories,
    parse_hashes,
)
from claude_hooks.providers.base import Memory


class TestParseHashes:
    def test_parses_hex(self):
        h, bad = parse_hashes(["00ff", "abcd"])
        assert h == [b"\x00\xff", b"\xab\xcd"]
        assert bad == []

    def test_accepts_0x_prefix_and_uppercase(self):
        h, bad = parse_hashes(["0xAABB"])
        assert h == [b"\xaa\xbb"] and bad == []

    def test_rejects_non_hex_rather_than_coercing(self):
        """'deleted 0' and 'your id was garbage' are different answers."""
        h, bad = parse_hashes(["zzzz"])
        assert h == [] and bad == ["zzzz"]

    def test_rejects_odd_length(self):
        h, bad = parse_hashes(["abc"])
        assert h == [] and bad == ["abc"]

    def test_deduplicates(self):
        h, _ = parse_hashes(["00ff", "00FF"])
        assert h == [b"\x00\xff"]

    def test_empty_input(self):
        assert parse_hashes([]) == ([], [])
        assert parse_hashes(None) == ([], [])

    def test_good_and_bad_are_separated(self):
        h, bad = parse_hashes(["00ff", "nope", "aa"])
        assert h == [b"\x00\xff", b"\xaa"]
        assert bad == ["nope"]


class TestFormatDeleteResult:
    def test_reports_the_count(self):
        assert "deleted 2 memories" in format_delete_result(2, 2, [])

    def test_singular(self):
        assert "deleted 1 memory" in format_delete_result(1, 1, [])

    def test_a_miss_is_said_out_loud(self):
        """The interesting case: the id was stale or from another store.
        Rounding that to success is how a failed delete looks fine."""
        out = format_delete_result(1, 3, [])
        assert "2 ids matched no row" in out

    def test_no_miss_language_when_all_matched(self):
        assert "matched no row" not in format_delete_result(3, 3, [])

    def test_malformed_ids_are_named(self):
        out = format_delete_result(0, 0, ["zzz"])
        assert "rejected 1 malformed" in out and "zzz" in out

    def test_long_reject_lists_are_truncated(self):
        out = format_delete_result(0, 0, [f"b{i}" for i in range(9)])
        assert "+4 more" in out


class TestRecallSurfacesAnId:
    def test_format_renders_the_id(self):
        mems = [Memory(text="a memory",
                       metadata={"_table": "memories_qwen3", "_hash": "dead"})]
        assert "id=dead" in format_memories(mems)

    def test_absent_id_is_omitted_not_rendered_empty(self):
        mems = [Memory(text="a memory", metadata={"_table": "t"})]
        assert "id=" not in format_memories(mems)

    def test_id_travels_with_score_and_distance(self):
        mems = [Memory(text="m", metadata={"_table": "t", "_score": 0.5,
                                           "_distance": 0.1, "_hash": "ab"})]
        out = format_memories(mems)
        assert "id=ab" in out and "score=" in out and "dist=" in out


def _server(**kw):
    from tests.test_pgvector_mcp import FakePgvectorProvider
    from claude_hooks.pgvector_mcp.server import McpServer

    p = FakePgvectorProvider()
    for k, v in kw.items():
        setattr(p, k, v)
    return McpServer(p), p


def _call(server, args):
    resp = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "pgvector-delete", "arguments": args},
    })
    return resp["result"]["content"][0]["text"], resp["result"].get("isError")


class TestDeleteTool:
    def test_deletes_and_reports(self):
        s, p = _server(delete_returns=2)
        text, err = _call(s, {"ids": ["00ff", "aabb"]})
        assert not err
        assert "deleted 2" in text
        assert p.delete_calls[0][0] == [b"\x00\xff", b"\xaa\xbb"]

    def test_defaults_to_every_table_recall_searches(self):
        """Otherwise a user deletes a kg_observations hit and is told it
        worked while the row stays put."""
        s, p = _server(delete_returns=1)
        _call(s, {"ids": ["00ff"]})
        assert p.delete_calls[0][1] == ["memories_qwen3", "kg_observations_qwen3"]

    def test_explicit_tables_are_honoured(self):
        s, p = _server(delete_returns=1)
        _call(s, {"ids": ["00ff"], "tables": ["memories_qwen3"]})
        assert p.delete_calls[0][1] == ["memories_qwen3"]

    def test_malformed_ids_do_not_reach_the_provider(self):
        s, p = _server()
        text, err = _call(s, {"ids": ["nonsense"]})
        assert not err
        assert p.delete_calls == []
        assert "rejected 1 malformed" in text

    def test_empty_ids_is_a_no_op_not_a_mass_delete(self):
        """The single most important negative case."""
        s, p = _server()
        text, err = _call(s, {"ids": []})
        assert not err
        assert p.delete_calls == []
        assert "deleted 0" in text

    def test_missing_ids_key_is_a_no_op(self):
        s, p = _server()
        _call(s, {})
        assert p.delete_calls == []

    def test_a_miss_is_reported_not_swallowed(self):
        s, _ = _server(delete_returns=0)
        text, err = _call(s, {"ids": ["00ff"]})
        assert not err
        assert "matched no row" in text

    def test_provider_failure_becomes_a_tool_error(self):
        s, p = _server()

        def boom(hashes, tables=None):
            raise RuntimeError("db is gone")

        p.delete_by_hashes = boom
        text, err = _call(s, {"ids": ["00ff"]})
        assert err
        assert "db is gone" in text


class TestCatalog:
    def test_delete_is_advertised_as_irreversible(self):
        """The description is the only warning a model gets before it
        calls this."""
        s, _ = _server()
        tools = s.handle({"jsonrpc": "2.0", "id": 1,
                          "method": "tools/list"})["result"]["tools"]
        d = next(t for t in tools if t["name"] == "pgvector-delete")
        assert "IRREVERSIBLE" in d["description"]
        assert d["inputSchema"]["required"] == ["ids"]
