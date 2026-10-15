"""Plan D11: peer findings must be about the search, and are given once."""
from __future__ import annotations

from types import SimpleNamespace

from consultants.engine import store as st


def _hit(text, lane=0, key="L0", sim=None):
    value = {"text": text, "lane_idx": lane}
    if sim is not None:
        value["_similarity"] = sim
    return SimpleNamespace(namespace=("s", "research"), key=key, value=value)


def test_content_words_and_gate():
    assert st.content_words("The caching of files, cached quickly") >= {"cach", "quickly"}
    q = "how is the token cache invalidated"
    assert st.is_about_the_search(q, "cache invalidation runs on token expiry")
    assert not st.is_about_the_search(q, "the logo is drawn in SVG")
    # One shared word is not enough when the query has more...
    assert not st.is_about_the_search(q, "the cache is warm")
    # ...unless the vector leg says the finding is close.
    assert st.is_about_the_search(q, "the cache is warm", similarity=0.62)
    assert not st.is_about_the_search(q, "the logo", similarity=0.31)


def test_clamp_keeps_head_and_tail():
    q = "HEAD " + "x" * 5000 + " TAIL"
    out = st.clamp_recall_query(q)
    assert len(out) <= st.RECALL_MAX_QUERY_CHARS
    assert out.startswith("HEAD") and out.endswith("TAIL")


def test_peer_findings_filters_and_gives_each_once(monkeypatch):
    hits = [_hit("token cache invalidated on expiry", lane=1, key="L1"),
            _hit("logo colours are set in theme.css", lane=2, key="L2"),
            _hit("own finding about the token cache", lane=0, key="L0")]
    monkeypatch.setattr(st, "recall_research", lambda *a, **k: list(hits))
    q = "token cache invalidation path"
    first = st.peer_findings(object(), "sid-d11", q, lane_idx=0)
    assert [h.key for h in first] == ["L1"]
    # Given once per lane per session...
    assert st.peer_findings(object(), "sid-d11", q, lane_idx=0) == []
    # ...but another lane still gets it, and an updated finding is new.
    assert [h.key for h in st.peer_findings(object(), "sid-d11", q,
                                            lane_idx=3)] == ["L1", "L0"]
    hits[0] = _hit("token cache invalidated on expiry and on logout", lane=1, key="L1")
    assert [h.key for h in st.peer_findings(object(), "sid-d11", q,
                                            lane_idx=0)] == ["L1"]


def test_short_queries_are_skipped(monkeypatch):
    monkeypatch.setattr(st, "recall_research",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert st.peer_findings(object(), "sid", "auth", lane_idx=0) == []


def test_only_a_cosine_distance_becomes_a_similarity():
    from claude_hooks.providers.pgvector import PgvectorProvider
    from claude_hooks.providers.sqlite_vec import SqliteVecProvider
    assert PgvectorProvider.vector_distance == "cosine"
    assert SqliteVecProvider.vector_distance == "l2"


def test_adapter_adds_similarity_only_for_a_cosine_provider():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_consultants_v2_store import _FakeMemory, _FakeProvider

    class _Distanced(_FakeProvider):
        def recall_hybrid(self, query, k=5):
            out = super().recall_hybrid(query, k)
            for m in out:
                m.metadata["_distance"] = 0.3
            return out

    for metric, want in (("cosine", 0.7), ("l2", None), (None, None)):
        provider = _Distanced()
        provider.vector_distance = metric
        store = st.ProviderBackedStore(provider)
        st.record_research(store, "sid-sim", lane_idx=1, plan_item="p",
                           finding="the token cache")
        hit = st.recall_research(store, "sid-sim", "token cache")[0]
        got = hit.value.get("_similarity")
        assert (round(got, 3) if got is not None else None) == want, metric
