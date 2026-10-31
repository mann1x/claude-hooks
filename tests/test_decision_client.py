"""Decision-model client: TypeSafe Jev + Ollama/xOllama /v1/systemone."""
from __future__ import annotations

import json

import pytest

from claude_hooks import decision_client as dc

OLLAMA = "http://box:11434"


@pytest.fixture(autouse=True)
def _fresh():
    dc.reset_caches()
    yield
    dc.reset_caches()


class Server:
    """A scripted server: ``script`` is a list of (status, body) for POSTs."""

    def __init__(self, *, version: str | None = None, script=None, tags=None):
        self.version = version
        self.script = list(script or [])
        self.tags = tags or []
        self.calls: list[tuple[str, str, dict, dict]] = []

    def __call__(self, method, url, body, headers, timeout):
        payload = json.loads(body) if body else {}
        self.calls.append((method, url, payload, dict(headers)))
        if url.endswith("/api/version"):
            if self.version is None:
                return 404, b"not found"
            return 200, json.dumps({"version": self.version}).encode()
        if url.endswith("/api/tags"):
            return 200, json.dumps({"models": self.tags}).encode()
        status, out = self.script.pop(0) if self.script else (200, {
            "model": payload.get("model"),
            "answers": {k: {"type": "noul", "noul": 0.9}
                        for k in payload.get("questions", {})}})
        return status, out if isinstance(out, bytes) else json.dumps(out).encode()

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def test_routes_accept_root_v1_and_full_path():
    for typed in (OLLAMA, OLLAMA + "/v1", OLLAMA + "/v1/systemone/"):
        r = dc.routes(typed)
        assert r.systemone == OLLAMA + "/v1/systemone"
        assert r.root == OLLAMA and r.custom
    r = dc.routes(None)
    assert r.systemone == "https://api.typesafe.ai/v1/systemone"
    assert not r.custom


def test_typesafe_sends_the_key_and_never_probes():
    srv = Server()
    ep = dc.Endpoint(api_key="sekret")
    out = dc.evaluate(ep, "s", {"q": dc.noul("is it?")}, transport=srv)
    assert out.answers["q"]["noul"] == 0.9 and out.dialect == "typesafe"
    assert [c[1] for c in srv.calls] == ["https://api.typesafe.ai/v1/systemone"]
    assert srv.calls[0][3]["Authorization"] == "Bearer sekret"


def test_typesafe_without_a_key_fails_before_sending(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    srv = Server()
    with pytest.raises(dc.DecisionError, match="needs an API key"):
        dc.evaluate(dc.Endpoint(), "s", {"q": dc.noul("x")}, transport=srv)
    assert not srv.calls


def test_key_file_is_read_and_env_never_reaches_a_custom_endpoint(
        tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("from-file\n")
    assert dc.Endpoint(key_file=str(key)).key() == "from-file"
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-only")
    assert dc.Endpoint().key() == "typesafe-only"
    assert dc.Endpoint(base_url=OLLAMA).key() is None
    srv = Server(version="0.40.3")
    dc.evaluate(dc.Endpoint(base_url=OLLAMA, model="tev1"), "s",
                {"q": dc.noul("x")}, transport=srv)
    assert "Authorization" not in srv.posts()[0][3]


def test_ollama_is_detected_once_and_cached():
    srv = Server(version="0.40.3")
    ep = dc.Endpoint(base_url=OLLAMA, model="tev1")
    assert dc.evaluate(ep, "s", {"q": dc.noul("x")}, transport=srv).dialect == "ollama"
    dc.evaluate(ep, "s", {"q": dc.noul("x")}, transport=srv)
    assert sum(c[1].endswith("/api/version") for c in srv.calls) == 1


def test_an_unreachable_probe_is_not_cached():
    def down(method, url, body, headers, timeout):
        raise ConnectionError("refused")
    assert dc.detect_dialect(dc.Endpoint(base_url=OLLAMA), transport=down) == "typesafe"
    srv = Server(version="0.40.3")
    assert dc.detect_dialect(dc.Endpoint(base_url=OLLAMA), transport=srv) == "ollama"


def test_ollama_body_is_fitted_under_64_kib_by_cutting_only_the_state():
    srv = Server(version="0.40.3")
    q = {"q": dc.noul("Does `doc` mention retries?")}
    dc.evaluate(dc.Endpoint(base_url=OLLAMA, model="tev1"),
                {"doc": "x" * 200_000}, q, transport=srv)
    method, url, payload, _ = srv.posts()[0]
    assert len(json.dumps(payload).encode()) <= dc.OLLAMA_MAX_BODY_BYTES
    assert payload["questions"] == q
    assert "chars cut" in payload["state"]["doc"]


def test_typesafe_body_is_never_cut():
    srv = Server()
    dc.evaluate(dc.Endpoint(api_key="k"), {"doc": "x" * 200_000},
                {"q": dc.noul("x")}, transport=srv)
    assert len(srv.posts()[0][2]["state"]["doc"]) == 200_000


def test_a_loaded_window_overflow_is_cut_retried_and_remembered():
    srv = Server(version="0.40.3", script=[
        (400, {"error": "prompt 0 has 12215 tokens; expected 1–2050"})])
    ep = dc.Endpoint(base_url=OLLAMA, model="tev1")
    state = {"doc": "word " * 8_000}
    out = dc.evaluate(ep, state, {"q": dc.noul("x")}, transport=srv)
    assert out.answers["q"]["noul"] == 0.9
    first, second = srv.posts()
    assert len(json.dumps(second[2]["state"])) < len(json.dumps(first[2]["state"])) / 4
    dc.evaluate(ep, state, {"q": dc.noul("x")}, transport=srv)
    third = srv.posts()[2]
    assert len(json.dumps(third[2]["state"])) <= len(json.dumps(second[2]["state"])) + 10


def test_more_than_26_options_fail_before_sending_on_ollama():
    srv = Server(version="0.40.3")
    options = {f"o{i}": None for i in range(27)}
    with pytest.raises(dc.DecisionError, match="at most 26"):
        dc.evaluate(dc.Endpoint(base_url=OLLAMA, model="tev1"), "s",
                    {"pick": dc.choice("which?", options)}, transport=srv)
    assert not srv.posts()


def test_only_429_and_529_are_retried():
    slept = []
    srv = Server(script=[(529, b"busy"), (429, b"slow")])
    out = dc.evaluate(dc.Endpoint(api_key="k"), "s", {"q": dc.noul("x")},
                      transport=srv, sleep=slept.append)
    assert out.answers and slept == [0.4, 1.2]
    srv = Server(script=[(422, {"detail": [{"msg": "Input should be a valid list"}]})])
    with pytest.raises(dc.DecisionError) as err:
        dc.evaluate(dc.Endpoint(api_key="k"), "s", {"q": dc.noul("x")},
                    transport=srv, sleep=slept.append)
    assert err.value.status == 422 and len(srv.posts()) == 1


def test_retries_stay_inside_one_deadline():
    srv = Server(script=[(529, b"busy")] * 3)
    with pytest.raises(dc.DecisionError, match="deadline"):
        dc.evaluate(dc.Endpoint(api_key="k", timeout_s=0.5), "s",
                    {"q": dc.noul("x")}, transport=srv, sleep=lambda s: None)


def test_ollama_404_names_the_model_to_pull():
    srv = Server(version="0.40.3", script=[(404, {"error": "model not found"})])
    with pytest.raises(dc.DecisionError, match="pull it first"):
        dc.evaluate(dc.Endpoint(base_url=OLLAMA, model="chef"), "s",
                    {"q": dc.noul("x")}, transport=srv)


def test_list_models_offers_only_decision_capable_ones_on_ollama():
    srv = Server(version="0.40.3", tags=[
        {"name": "tev1:latest", "capabilities": ["decision"]},
        {"name": "qwen3:8b", "capabilities": ["completion", "tools"]},
        {"name": "chef:latest", "capabilities": ["decision"]}])
    assert dc.list_models(dc.Endpoint(base_url=OLLAMA), transport=srv) == [
        "chef:latest", "tev1:latest"]


def test_confidence_reads_all_three_kinds_on_one_scale():
    assert dc.confidence({"type": "noul", "noul": 0.9}) == pytest.approx(0.8)
    assert dc.confidence({"type": "noul", "noul": 0.1}) == pytest.approx(0.8)
    assert dc.confidence({"type": "choice", "confidence": 0.71}) == 0.71
    assert not dc.confident({"type": "noul", "noul": 0.75})   # 0.5 < 0.6
    assert dc.confident({"type": "score", "confidence": 0.99})


def test_score_criteria_are_an_array():
    # Both TypeSafe (422 list_type) and Ollama (400) refuse a map.
    assert dc.score("how bad?", ["low", "high"])["criteria"] == ["low", "high"]
