"""
S2 tests — request-body parser extensions + agent classification.

Fixtures are synthetic but mirror the live request shape captured
from Claude Code 2.1.107.616 (see docs/PLAN-stats-sqlite.md S2).
"""

from __future__ import annotations

import json

from claude_hooks.proxy.metadata import (
    extract_request_info,
    _extract_account_uuid,
    _extract_agent_name,
    _extract_cc_billing,
)


def _main_agent_body(overrides=None) -> bytes:
    body = {
        "model": "claude-opus-4-6",
        "stream": True,
        "max_tokens": 64000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "context_management": {"edits": []},
        "metadata": {
            "user_id": json.dumps({
                "device_id": "dev123",
                "account_uuid": "acc-uuid-abc",
                "session_id": "sess-xyz",
            }),
        },
        "system": [
            {"type": "text",
             "text": "x-anthropic-billing-header: cc_version=2.1.107.616; cc_entrypoint=cli; cch=aabbccdd;"},
            {"type": "text",
             "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": "Full instructions here..."},
        ],
        "tools": [{"name": f"t{i}"} for i in range(42)],
        "messages": [
            {"role": "user", "content": "Hello world"},
        ],
    }
    if overrides:
        body.update(overrides)
    return json.dumps(body).encode()


# ============================================================ #
class TestMainAgentClassification:
    def test_main_agent_full_extraction(self):
        info = extract_request_info(
            _main_agent_body(),
            {"anthropic-beta": "context-management-2025-06-27,oauth-2025-04-20"},
        )
        assert info["model_requested"] == "claude-opus-4-6"
        assert info["stream"] is True
        assert info["max_tokens"] == 64000
        assert info["thinking_type"] == "adaptive"
        assert info["effort"] == "medium"
        assert info["num_tools"] == 42
        assert info["num_messages"] == 1
        assert info["account_uuid"] == "acc-uuid-abc"
        assert info["cc_version"] == "2.1.107.616"
        assert info["cc_entrypoint"] == "cli"
        assert info["agent_type"] == "main"
        assert info["agent_name"] == "main"
        assert info["request_class"] == "main"
        assert info["beta_features"] == [
            "context-management-2025-06-27", "oauth-2025-04-20",
        ]


class TestWarmup:
    def test_warmup_detected_and_classified(self):
        body = _main_agent_body({
            "messages": [{"role": "user", "content": "Warmup"}],
            "system": [
                {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.107.616;"},
                {"type": "text", "text": "You are a subagent doing warmup..."},
            ],
        })
        info = extract_request_info(body, {})
        assert info["is_warmup"] is True
        assert info["agent_type"] == "warmup"
        assert info["agent_name"] == "warmup"
        assert info["request_class"] == "warmup"


class TestSubagent:
    def test_subagent_name_extracted(self):
        body = _main_agent_body({
            "system": [
                {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.107.616;"},
                {"type": "text",
                 "text": "You are a code reviewer specialized in Python security audits."},
            ],
        })
        info = extract_request_info(body, {})
        assert info["agent_type"] == "subagent"
        # "a code reviewer" → "a code reviewer" stripped; helper cleans
        # the leading article and cuts at "specialized".
        assert info["agent_name"]
        assert "code reviewer" in info["agent_name"]
        assert info["request_class"] == "subagent"

    def test_subagent_different_shapes(self):
        cases = [
            "You are an ultrathink detective analyzing bug chains.",
            "You are the General-Purpose research agent.",
            "You are a developer-detective who traces data flow.",
        ]
        for persona in cases:
            body = _main_agent_body({
                "system": [
                    {"type": "text",
                     "text": "x-anthropic-billing-header: cc_version=2.1.107.616;"},
                    {"type": "text", "text": persona},
                ],
            })
            info = extract_request_info(body, {})
            assert info["agent_type"] == "subagent", persona
            assert info["agent_name"] is not None, persona
            assert 1 <= len(info["agent_name"]) <= 60


class TestMalformedInputs:
    def test_empty_body_returns_defaults(self):
        info = extract_request_info(b"", {})
        assert info["model_requested"] is None
        assert info["agent_type"] is None
        assert info["is_warmup"] is False

    def test_invalid_json_only_beta_header_survives(self):
        info = extract_request_info(
            b"not json",
            {"anthropic-beta": "features-X-2025-01-01"},
        )
        assert info["beta_features"] == ["features-X-2025-01-01"]
        assert info["model_requested"] is None

    def test_non_dict_body_returns_defaults(self):
        info = extract_request_info(b"[1,2,3]", {})
        assert info["model_requested"] is None

    def test_missing_system_block(self):
        body = json.dumps({"model": "x", "messages": []}).encode()
        info = extract_request_info(body, {})
        assert info["cc_version"] is None
        assert info["agent_type"] == "unknown"


# ============================================================ #
class TestHelpers:
    def test_extract_account_uuid_json_form(self):
        j = json.dumps({"device_id": "d", "account_uuid": "aaa", "session_id": "s"})
        assert _extract_account_uuid(j) == "aaa"

    def test_extract_account_uuid_legacy_form(self):
        legacy = "user_devhash_account_66b5f862-97a6-462a-96d2-941d7e8b79d8_session_aaa"
        assert _extract_account_uuid(legacy) == "66b5f862-97a6-462a-96d2-941d7e8b79d8"

    def test_extract_account_uuid_none(self):
        assert _extract_account_uuid(None) is None
        assert _extract_account_uuid("garbage") is None
        assert _extract_account_uuid("{bad-json") is None

    def test_extract_cc_billing_from_string(self):
        s = "x-anthropic-billing-header: cc_version=2.1.107.616; cc_entrypoint=cli; cch=x;"
        v, e = _extract_cc_billing(s)
        assert v == "2.1.107.616"
        assert e == "cli"

    def test_extract_cc_billing_missing(self):
        assert _extract_cc_billing(None) == (None, None)
        assert _extract_cc_billing("no billing tag here") == (None, None)

    def test_extract_agent_name_strips_leading_article(self):
        assert _extract_agent_name("You are a code reviewer.") == "code reviewer"
        assert _extract_agent_name("You are an ultrathink detective.") == "ultrathink detective"
        assert _extract_agent_name("You are the General agent.") == "the General agent"
        # Not a "You are" prompt → None.
        assert _extract_agent_name("Helper module.") is None
