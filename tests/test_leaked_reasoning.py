"""glm-5.3-flash asked with ``think: false`` writes its reasoning into the
answer and ends it with ``</think>`` (2026-10-27 bench: 8 of 9 forced
calls); the client moves that block to ``thinking``."""
from __future__ import annotations

import pytest

from claude_hooks.get_advice import chat_client as C

# The shape of glm-5-3-flash-cloud-2026-10-27-r2 audit-medium's synthesizer.
LEAK = ("Let me synthesize the final answer. I have two researcher reports "
        "plus my own tool verifications.\n\nCONFIDENCE line at end."
        "</think>**Bottom line: four sites lack the fallback.**\n\n- a")


class TestSplit:
    def test_the_bench_leak(self):
        reasoning, answer = C.split_leaked_reasoning(LEAK)
        assert reasoning.startswith("Let me synthesize")
        assert reasoning.endswith("CONFIDENCE line at end.")
        assert answer.startswith("**Bottom line")

    def test_a_leading_open_tag_is_dropped(self):
        got = C.split_leaked_reasoning("  <think>\nplan it\n</think>\n\n1. x")
        assert got == ("plan it", "1. x")

    def test_reasoning_with_no_answer_leaves_an_empty_answer(self):
        assert C.split_leaked_reasoning("still thinking</think>") == \
            ("still thinking", "")

    @pytest.mark.parametrize("content", [
        "",
        "1. A plan with no tags at all.",
        "The reply puts it in the content as `<think>…</think>` first.",
        "Ends the leak with `</think>`, which the client splits.",
        "Inline mode writes <think>x</think> ahead of the reply.",
        "</think> with nothing before it",
    ])
    def test_anything_else_is_left_alone(self, content):
        assert C.split_leaked_reasoning(content) is None


def _reply(content, thinking=None):
    msg = {"role": "assistant", "content": content}
    if thinking is not None:
        msg["thinking"] = thinking
    return {"model": "glm-5.3-flash", "message": msg, "done_reason": "stop",
            "prompt_eval_count": 10, "eval_count": 5}


class TestTheClient:
    def test_the_answer_loses_the_reasoning(self):
        out = C.ChatClient("http://127.0.0.1:9")._from_ollama(_reply(LEAK))
        msg = out["choices"][0]["message"]
        assert msg["content"].startswith("**Bottom line")
        assert "</think>" not in msg["content"]
        assert msg["thinking"].startswith("Let me synthesize")

    def test_server_reasoning_is_kept_ahead_of_it(self):
        out = C.ChatClient("http://127.0.0.1:9")._from_ollama(
            _reply(LEAK, thinking="served"))
        assert out["choices"][0]["message"]["thinking"].startswith(
            "served\n\nLet me synthesize")

    def test_a_clean_answer_is_untouched(self):
        reply = _reply("**Bottom line.**", thinking="t")
        msg = C.ChatClient("http://127.0.0.1:9")._from_ollama(reply)[
            "choices"][0]["message"]
        assert msg == {"role": "assistant", "content": "**Bottom line.**",
                       "thinking": "t"}

    def test_the_openai_wire_too(self):
        c = C.OpenAICompatChatClient("http://127.0.0.1:9", backend="llamacpp")
        data = c._decode({"choices": [{"message": {
            "role": "assistant", "content": LEAK},
            "finish_reason": "stop"}], "usage": {}})
        msg = c._from_ollama(data)["choices"][0]["message"]
        assert msg["content"].startswith("**Bottom line")
