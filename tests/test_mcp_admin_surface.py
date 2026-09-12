"""The rest of the memory-administration surface: list, replace, TTL,
and KG removal/enumeration.

Each of these closed a hole where the store could be written to but not
corrected. The recurring shape is that the *read* side and the *write*
side disagreed about what was reachable — recall spanned tables delete
did not, search could find entities nothing could remove, and count
reported a corpus size nothing could page through.
"""
from typing import Optional

import pytest

from claude_hooks.mcp_format import format_graph, format_kg_delete_result
from claude_hooks.pgvector_mcp.server import McpServer
from claude_hooks.providers.base import Memory, Provider


class FakeProv:
    name = "pgvector"
    display_name = "fake"

    def __init__(self):
        self.tables = ["memories_qwen3", "kg_observations_qwen3"]
        self.deleted: list = []
        self.stored: list = []
        self.listed: list = []
        self.refreshed: list = []
        self.delete_returns = 1
        self.kg_delete_entities_returns = {
            "entities": 1, "observations": 12, "relations": 3, "missing": []}
        self.kg_obs_deleted: list = []
        self.kg_rel_deleted: list = []
        self.graph: list = []
        self.opened: list = []
        self.expiring: list = []

    def _resolve_tables(self): return list(self.tables)

    def delete_by_hashes(self, hashes, tables=None):
        self.deleted.append((list(hashes), tables)); return self.delete_returns

    def store(self, content, metadata=None):
        self.stored.append((content, dict(metadata or {})))

    def list_memories(self, limit=20, offset=0, table=None):
        self.listed.append((limit, offset, table))
        return [Memory(text="row one",
                       metadata={"_table": "memories_qwen3", "_hash": "aa"})]

    def expire_before(self, *, before_iso, limit=50):
        self.expiring.append((before_iso, limit)); return list(self.expiring_rows)

    expiring_rows: list = []

    def refresh_expires_at(self, chash, new_iso):
        self.refreshed.append((chash, new_iso))

    def kg_delete_entities(self, names):
        self.kg_names = list(names); return dict(self.kg_delete_entities_returns)

    def kg_delete_observations(self, items):
        self.kg_obs_deleted.extend(items); return len(items)

    def kg_delete_relations(self, rels):
        self.kg_rel_deleted.extend(rels); return len(rels)

    def kg_read_graph(self, limit=100):
        self.graph_limit = limit; return list(self.graph)

    def kg_open_nodes(self, names):
        self.opened.extend(names)
        return [{"name": n, "entity_type": "t", "observations": ["o1"],
                 "relations": [], "_score": 1.0, "_match": "exact"}
                for n in names]


def _srv(**kw):
    p = FakeProv()
    for k, v in kw.items():
        setattr(p, k, v)
    return McpServer(p), p


def _call(server, tool, args):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}})
    return r["result"]["content"][0]["text"], r["result"]["isError"]


class TestList:
    def test_lists_with_defaults(self):
        s, p = _srv()
        text, err = _call(s, "pgvector-list", {})
        assert not err and "row one" in text
        assert p.listed == [(20, 0, None)]

    def test_paging_is_passed_through(self):
        s, p = _srv()
        _call(s, "pgvector-list", {"limit": 5, "offset": 40})
        assert p.listed == [(5, 40, None)]

    def test_listed_rows_carry_their_id(self):
        """A listing you cannot act on is just a wall of text."""
        s, _ = _srv()
        text, _ = _call(s, "pgvector-list", {})
        assert "id=aa" in text


class TestReplace:
    def test_deletes_then_stores(self):
        s, p = _srv()
        text, err = _call(s, "pgvector-replace",
                          {"id": "aa", "content": "the corrected fact"})
        assert not err
        assert p.deleted and p.stored
        assert p.stored[0][0] == "the corrected fact"
        assert "replaced 1" in text

    def test_warns_when_the_id_matched_nothing(self):
        """Otherwise 'replace' silently degrades into 'store', leaving
        the wrong memory in place next to its correction."""
        s, _ = _srv(delete_returns=0)
        text, err = _call(s, "pgvector-replace", {"id": "aa", "content": "new"})
        assert not err
        assert "WARNING" in text and "only stored" in text

    def test_empty_content_is_refused(self):
        """Replacing with nothing is a delete wearing a disguise."""
        s, p = _srv()
        _text, err = _call(s, "pgvector-replace", {"id": "aa", "content": "   "})
        assert err
        assert p.stored == []

    def test_malformed_id_does_not_store(self):
        s, p = _srv()
        _call(s, "pgvector-replace", {"id": "zz!", "content": "new"})
        assert p.stored == [] and p.deleted == []


class TestTtl:
    def test_expiring_is_read_only(self):
        class Row:
            content_hash = b"\xaa"
            content = "about to expire"
            expires_at = "2026-01-01T00:00:00Z"
        s, p = _srv(expiring_rows=[Row()])
        text, err = _call(s, "pgvector-expiring", {"before": "2026-02-01T00:00:00Z"})
        assert not err
        assert "about to expire" in text and "id=aa" in text
        assert p.deleted == []          # showing is not taking

    def test_expiring_requires_an_instant(self):
        s, _ = _srv()
        _text, err = _call(s, "pgvector-expiring", {})
        assert err

    def test_refresh_pushes_the_expiry_out(self):
        s, p = _srv()
        text, err = _call(s, "pgvector-refresh-ttl",
                          {"id": "aa", "expires_at": "2027-01-01T00:00:00Z"})
        assert not err
        assert p.refreshed == [(b"\xaa", "2027-01-01T00:00:00Z")]
        assert "2027" in text


class TestKgRemoval:
    def test_entity_delete_reports_the_cascade(self):
        """The entity count alone understates it by two orders of
        magnitude — the observations are what took work to accumulate."""
        s, _ = _srv()
        text, err = _call(s, "pgvector-kg-delete-entities", {"names": ["solidPC"]})
        assert not err
        assert "12 observations" in text and "3 relations" in text

    def test_missing_names_are_named(self):
        s, _ = _srv(kg_delete_entities_returns={
            "entities": 0, "observations": 0, "relations": 0,
            "missing": ["ghost"]})
        text, _ = _call(s, "pgvector-kg-delete-entities", {"names": ["ghost"]})
        assert "ghost" in text and "matched no entity" in text

    def test_observation_delete_uses_the_kg_observe_shape(self):
        s, p = _srv()
        _call(s, "pgvector-kg-delete-observations",
              {"items": [{"entity_name": "solidPC", "content": "has 12 cores"}]})
        assert p.kg_obs_deleted == [
            {"entity_name": "solidPC", "content": "has 12 cores"}]

    def test_relation_delete_uses_the_kg_relate_shape(self):
        s, p = _srv()
        _call(s, "pgvector-kg-delete-relations",
              {"relations": [{"from": "a", "to": "b", "relation_type": "runs"}]})
        assert p.kg_rel_deleted[0]["relation_type"] == "runs"

    def test_empty_input_deletes_nothing(self):
        s, p = _srv()
        for tool, key in (("pgvector-kg-delete-entities", "names"),
                          ("pgvector-kg-delete-observations", "items"),
                          ("pgvector-kg-delete-relations", "relations")):
            _call(s, tool, {key: []})
        assert p.kg_obs_deleted == [] and p.kg_rel_deleted == []


class TestKgEnumeration:
    def test_read_graph_lists_entities_with_counts(self):
        s, _ = _srv(graph=[{"name": "solidPC", "entity_type": "server",
                            "observation_count": 12, "relation_count": 3}])
        text, err = _call(s, "pgvector-kg-read-graph", {})
        assert not err
        assert "solidPC" in text and "observations=12" in text

    def test_empty_graph_says_so_rather_than_nothing(self):
        s, _ = _srv(graph=[])
        text, _ = _call(s, "pgvector-kg-read-graph", {})
        assert "empty graph" in text

    def test_open_nodes_is_exact_match(self):
        s, p = _srv()
        text, err = _call(s, "pgvector-kg-open-nodes", {"names": ["solidPC"]})
        assert not err
        assert p.opened == ["solidPC"]
        assert "solidPC" in text and "o1" in text


class TestFormatters:
    def test_kg_delete_singular_plural(self):
        one = format_kg_delete_result(
            {"entities": 1, "observations": 1, "relations": 1, "missing": []})
        assert "1 entity" in one and "1 observation" in one

    def test_graph_empty(self):
        assert format_graph([]) == "(empty graph)"


class TestBaseProviderContract:
    """A provider that cannot do these must raise, not return empty —
    an empty list reads as 'the store has nothing', which is a lie."""

    @pytest.mark.parametrize("op,args", [
        ("kg_delete_entities", ([],)),
        ("kg_delete_observations", ([],)),
        ("kg_delete_relations", ([],)),
        ("kg_read_graph", ()),
        ("kg_open_nodes", ([],)),
        ("list_memories", ()),
    ])
    def test_unsupported_ops_raise(self, op, args):
        class Bare(Provider):
            name = "bare"
            @classmethod
            def detect(cls, c): return []
            @classmethod
            def signature_tools(cls): return set()
            def recall(self, q, k=5): return []
            def store(self, c, metadata=None, **kw): return None

        p = Bare.__new__(Bare)
        p.name = "bare"
        with pytest.raises(NotImplementedError):
            getattr(p, op)(*args)
