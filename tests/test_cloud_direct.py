"""``[cloud] route``: Ollama Cloud models go to ollama.com directly by
default, under their bare names, so a cloud council holds no host's
Ollama (2026-10-27); ``local`` keeps the host's Ollama relaying them."""
from __future__ import annotations

import pytest

from claude_hooks.get_advice import chat_client as C
from consultants import config as cc
from consultants.server import runner

LOCAL = "http://127.0.0.1:9"


def _eps(**cloud):
    cfg = cc.ConsultantsConfig()
    for k, v in cloud.items():
        setattr(cfg.cloud, k, v)
    return cc.client_endpoints(cfg)


class TestBareCloudName:
    @pytest.mark.parametrize("name,bare", [
        ("kimi-k2.6:cloud", "kimi-k2.6"),
        ("glm-5.3-flash:cloud", "glm-5.3-flash"),
        ("gemma4:31b-cloud", "gemma4:31b"),
        ("qwen3-coder:480b-cloud", "qwen3-coder:480b"),
    ])
    def test_cloud_names(self, name, bare):
        assert C.bare_cloud_name(name) == bare

    @pytest.mark.parametrize("name", [
        "qwen3:8b", "gemma4:31b", "my-cloud", "x:-cloud", ":cloud", "",
        "@ollamacom/glm-5.3", "llamafile://x:cloud", None,
    ])
    def test_anything_else_is_not(self, name):
        assert C.bare_cloud_name(name) is None


class TestTheConfig:
    def test_direct_is_the_shipped_default(self):
        cfg = cc.ConsultantsConfig()
        assert cfg.cloud.route == "direct"
        (name, ep), = cc.client_endpoints(cfg).items()
        assert name == cc.CLOUD_ENDPOINT and ep.cloud_models
        assert ep.url == "https://ollama.com" and ep.backend == "ollama"

    def test_local_adds_nothing(self):
        assert _eps(route="local") == {}

    def test_a_user_endpoint_keeps_its_name(self):
        cfg = cc.ConsultantsConfig()
        mine = cc.EndpointConfig(url="http://box:8080")
        cfg.endpoints[cc.CLOUD_ENDPOINT] = mine
        eps = cc.client_endpoints(cfg)
        assert eps[cc.CLOUD_ENDPOINT] is mine and not mine.cloud_models
        assert eps[cc.CLOUD_ENDPOINT + "-direct"].cloud_models
        assert cfg.endpoints == {cc.CLOUD_ENDPOINT: mine}

    def test_the_default_is_not_written(self):
        assert "[cloud]" not in cc._render(cc.ConsultantsConfig())

    def test_the_file_round_trips(self):
        cfg = cc.ConsultantsConfig()
        cfg.cloud.route = "local"
        cfg.cloud.key_file = "~/.claude/other-key"
        text = cc._render(cfg)
        assert '[cloud]\n' in text and 'route = "local"' in text
        import tomllib
        got = cc._merge_layer(cc.ConsultantsConfig(), tomllib.loads(text))
        assert got.cloud == cfg.cloud

    @pytest.mark.parametrize("raw,route", [
        ({"route": "local"}, "local"), ({"route": "DIRECT"}, "direct"),
        ({"route": "relay"}, "direct"), ({}, "direct"),
    ])
    def test_the_loader(self, raw, route):
        got = cc._merge_layer(cc.ConsultantsConfig(), {"cloud": raw})
        assert got.cloud.route == route

    def test_a_later_layer_without_cloud_keeps_the_earlier_route(self):
        base = cc._merge_layer(cc.ConsultantsConfig(),
                               {"cloud": {"route": "local"}})
        assert cc._merge_layer(base, {"critic": {}}).cloud.route == "local"

    def test_set_cloud_refuses_a_bad_route_or_url(self):
        with pytest.raises(ValueError):
            cc.set_cloud(route="relay")
        with pytest.raises(ValueError):
            cc.set_cloud(url="ollama.com")


class TestTheClient:
    def test_a_cloud_name_goes_to_ollama_com_under_its_bare_name(self):
        c = C.make_agent_chat_client("gemma4:31b-cloud", LOCAL,
                                     endpoints=_eps())
        assert c.base_url == "https://ollama.com" and c.cloud_direct
        assert c._real("gemma4:31b-cloud") == "gemma4:31b"
        body = c._with_served_model({"model": "kimi-k2.6:cloud"})
        assert body["model"] == "kimi-k2.6"

    def test_local_route_keeps_the_host(self):
        c = C.make_agent_chat_client("gemma4:31b-cloud", LOCAL,
                                     endpoints=_eps(route="local"))
        assert c.base_url == LOCAL and not c.cloud_direct
        assert c._real("gemma4:31b-cloud") == "gemma4:31b-cloud"

    def test_a_local_model_stays_on_the_host(self):
        c = C.make_agent_chat_client("qwen3:8b", LOCAL, endpoints=_eps())
        assert c.base_url == LOCAL and not c.cloud_direct

    def test_an_endpoint_reference_is_unchanged(self):
        eps = {**_eps(), "box": cc.EndpointConfig(url="http://box:11434",
                                                  backend="xollama")}
        c = C.make_agent_chat_client("@box/glm-5.3", LOCAL, endpoints=eps)
        assert c.base_url == "http://box:11434" and not c.cloud_direct
        assert c._real("@box/glm-5.3") == "glm-5.3"

    def test_a_role_mixing_local_and_cloud_routes_each_call(self):
        c = C.make_role_chat_client(["qwen3:8b", "glm-5.3-flash:cloud"],
                                    LOCAL, endpoints=_eps())
        assert isinstance(c, C.ModelRoutingChatClient)
        assert c._for("qwen3:8b").base_url == LOCAL
        assert c._for("glm-5.3-flash:cloud").base_url == "https://ollama.com"
        assert c._for("gemma4:31b-cloud") is c._for("glm-5.3-flash:cloud")

    def test_an_all_cloud_role_gets_one_plain_client(self):
        c = C.make_role_chat_client(["gemma4:31b-cloud", "kimi-k2.6:cloud"],
                                    LOCAL, endpoints=_eps())
        assert isinstance(c, C.ChatClient) and c.cloud_direct

    def test_the_council_roles_use_the_route(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["critic"].model = "gemma4:31b-cloud"
        assert runner._role_client(cfg, "critic", LOCAL).base_url \
            == "https://ollama.com"
        cfg.cloud.route = "local"
        assert runner._role_client(cfg, "critic", LOCAL).base_url == LOCAL

    def test_the_key_file_is_the_endpoints(self, tmp_path):
        kf = tmp_path / "k"
        kf.write_text("test-only-not-a-real-key\n")
        c = C.make_agent_chat_client("gemma4:31b-cloud", LOCAL,
                                     endpoints=_eps(key_file=str(kf)))
        assert c.api_key == "test-only-not-a-real-key"
