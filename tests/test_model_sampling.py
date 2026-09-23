"""Sampling templates: shipped file + user overrides, sent on the wire."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from claude_hooks import model_sampling as ms  # noqa: E402

SHIPPED = {"glm-5.3*": {"temperature": 0.7, "_why": "measured"},
           "*": {"top_k": 40}}


def _user(templates):
    return {"model_sampling": {"templates": templates}}


def test_the_shipped_file_is_valid_and_measured_only():
    data = json.loads(ms.SHIPPED_FILE.read_text(encoding="utf-8"))
    table = ms.templates(cfg={}, shipped=data["templates"])
    assert table["glm-5.3*"] == {"temperature": 0.7}
    for opts in table.values():
        assert set(opts) <= ms.FIELDS


def test_most_specific_pattern_wins():
    table = ms.templates(cfg={}, shipped=SHIPPED)
    assert ms.match("glm-5.3-flash:cloud", table) == "glm-5.3*"
    assert ms.match("gemma4:31b-cloud", table) == "*"


def test_user_fields_merge_over_shipped_ones():
    table = ms.templates(cfg=_user({"glm-5.3*": {"top_p": 0.95}}),
                         shipped=SHIPPED)
    assert table["glm-5.3*"] == {"temperature": 0.7, "top_p": 0.95}


def test_null_drops_a_field_and_disables_a_pattern():
    table = ms.templates(
        cfg=_user({"glm-5.3*": {"temperature": None}, "*": None}),
        shipped=SHIPPED)
    assert table["glm-5.3*"] == {}
    assert "*" not in table


def test_a_user_pattern_adds_a_template():
    table = ms.templates(cfg=_user({"deepseek-v4.1-flash*": {"temperature": 0.6}}),
                         shipped=SHIPPED)
    assert ms.match("deepseek-v4.1-flash:cloud", table) == "deepseek-v4.1-flash*"


def test_unknown_fields_are_dropped_not_sent(caplog):
    table = ms.templates(cfg=_user({"x*": {"temprature": 0.5}}), shipped={})
    assert table["x*"] == {}
    assert "temprature" in caplog.text


def test_the_chat_client_sends_the_template_and_lets_callers_win(monkeypatch):
    from claude_hooks.get_advice import chat_client as cc
    monkeypatch.setattr(ms, "templates",
                        lambda cfg=None, shipped=None: {"glm-5.3*": {"temperature": 0.7}})
    client = cc.ChatClient.__new__(cc.ChatClient)

    def opts(payload):
        return client._to_ollama(payload).get("options")

    assert opts({"model": "glm-5.3:cloud", "messages": []}) == {"temperature": 0.7}
    assert opts({"model": "glm-5.3:cloud", "messages": [],
                 "options": {"temperature": 0.2}}) == {"temperature": 0.2}
    # None asks for the provider default: nothing on the wire.
    assert opts({"model": "glm-5.3:cloud", "messages": [],
                 "options": {"temperature": None}}) is None
    assert opts({"model": "gemma4:31b-cloud", "messages": []}) is None


def test_label_records_the_sampling():
    assert ms.label("m", {}) == "m"
    lab = ms.label("m:cloud", {"top_p": 0.9, "temperature": 0.7})
    assert lab == "m:cloud@temperature=0.7,top_p=0.9"
    assert ms.model_of(lab) == "m:cloud"


def test_cli_options_parse_as_json_and_reject_typos():
    import pytest
    assert ms.parse_options(["repeat_last_n=2048", "repeat_penalty=1.1",
                             "top_p=null"]) == {
        "repeat_last_n": 2048, "repeat_penalty": 1.1, "top_p": None}
    for bad in (["repeat_penlty=1.1"], ["temperature"], ["top_k=high"]):
        with pytest.raises(ValueError):
            ms.parse_options(bad)


def test_the_env_layer_merges_over_the_user_file_on_the_live_path(monkeypatch):
    monkeypatch.setattr(ms, "_cached_json", lambda which: (
        _user({"glm-5.3*": {"top_p": 0.95}}) if which == "user"
        else {"templates": SHIPPED}))
    monkeypatch.setenv(ms.ENV_VAR, '{"glm-5.3*": {"repeat_penalty": 1.1}}')
    assert ms.sampling_for("glm-5.3-flash:cloud") == {
        "temperature": 0.7, "top_p": 0.95, "repeat_penalty": 1.1}
    # An explicit cfg is not the live path: the env does not reach it.
    assert "repeat_penalty" not in ms.templates(cfg={}, shipped=SHIPPED)["glm-5.3*"]
