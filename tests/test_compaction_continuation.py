"""Plan F3: compaction as a continuation of the lane.

The writer and the half-critics run as the lane's next turns — its own
messages, tools and options, then the instruction — so a server with the
lane's prefix resident (a PolyKV layer, a prompt cache) prefills only the
instruction. The retrospective and the synthesizer stay requests of
their own. Off (and outside PolyKV under ``auto``) nothing changes.
"""
from __future__ import annotations

import threading

import pytest

from consultants.engine import compaction, polykv
from consultants.engine import compaction_prompts as P

from claude_hooks.agent_loop.runner import HARNESS_TAG as TAG
from tests.test_compaction_flows import Backend, _loop_history, _plan

STEP_SYSTEMS = (P.WRITER_SYSTEM_PROMPT, P.FULL_SYSTEM_PROMPT,
                P.CRITIC_SYSTEM_PROMPT, P.REVIEWER_SYSTEM_PROMPT,
                P.RETROSPECTIVE_SYSTEM_PROMPT, P.SYNTHESIZER_SYSTEM_PROMPT)

TOOLS = [{"type": "function", "function": {"name": "read_file",
                                           "parameters": {"type": "object"}}}]


def step_of(payload) -> str:
    """Which compaction step a payload is: by its system prompt, or by
    the system prompt that opens its last user turn (a continuation)."""
    first = payload["messages"][0]["content"]
    if first in STEP_SYSTEMS:
        return first
    # The instruction rides on the last lane message; a re-ask after a
    # tool call has the call and its answer after it.
    for m in reversed(payload["messages"][-3:]):
        text = str(m.get("content") or "")
        hit = next((s for s in STEP_SYSTEMS if f"{TAG} {s}" in text), None)
        if hit:
            return hit
    return "lane"


class ContBackend(Backend):
    """``Backend`` answering continuation payloads as well."""

    def __init__(self, ctx=100_000, *, writer_text=None, **kw):
        # A window the lane's history still fits in, as at a real trigger
        # (the shared 82k-token loop overruns Backend's 32k default).
        super().__init__(ctx, **kw)
        self.writer_text = writer_text

    def _reply(self, payload):
        step = step_of(payload)
        if step == payload["messages"][0]["content"]:
            return super()._reply(payload)
        if step in (P.WRITER_SYSTEM_PROMPT, P.FULL_SYSTEM_PROMPT):
            if self.writer_text is not None:
                return self.writer_text
            return ("The user is asking me to find the bug. Let me read the "
                    "file. [#1] It shows the loop.\n<<<HALFWAY>>>\nNow I am "
                    "checking the caller. [#2] It passes None. I am about to "
                    "fix the guard.")
        if step == P.CRITIC_SYSTEM_PROMPT:
            user = payload["messages"][-1]["content"]
            return ("The user is asking me to find the bug. Let me read the "
                    "file. [#1] It shows the loop."
                    if "**first half**" in user else
                    "Now I am checking the caller. [#2] It passes None. I am "
                    "about to fix the guard.")
        if step == P.REVIEWER_SYSTEM_PROMPT:
            return ("## Replay\nThe user is asking me to find the bug. [#1] "
                    "It shows the loop. [#2] It passes None, so I am fixing "
                    "the guard.\n\n## Retrospective\nReading the caller first.")
        return "ok"


class FakeManager:
    def __init__(self, continues=True):
        self._continues = continues
        self.erased: list[str] = []
        self.order: list[str] = []

    def continues(self, client, model):
        return self._continues

    def erase_session(self, sid):
        self.erased.append(sid)
        self.order.append("erase")
        return True


class Placed:
    """A PlacedChat stand-in: records the step order next to the erase."""

    def __init__(self, inner, mgr, session="lane-sess"):
        self._placed_inner = inner
        self._placed_manager = mgr
        self._session = session

    def chat(self, payload):
        self._placed_manager.order.append(step_of(payload))
        return self._placed_inner.chat(payload)

    def last_placement(self):
        return {"session_id": self._session}

    def context_length(self, model):
        return self._placed_inner.context_length(model)


def _scope(client, *, flow="full", continuation="auto", **kw):
    return compaction.Scope(
        chat_client=client, model="lane-model", role="researcher",
        settings=compaction.Settings(flow=flow, continuation=continuation),
        tools=TOOLS, **kw)


def _run(flow="full", continuation="auto", mgr=None, backend=None, **kw):
    backend = backend or ContBackend()
    mgr = mgr or FakeManager()
    client = Placed(backend, mgr)
    msgs = _loop_history()
    scope = _scope(client, flow=flow, continuation=continuation, **kw)
    out = compaction.compact(scope, msgs, plan=_plan(backend, msgs),
                             tools=TOOLS)
    return out, backend, mgr, msgs


def by_step(backend, step):
    return [p for p in backend.payloads if step_of(p) == step]


# --- the gate ----------------------------------------------------------- #

class TestGate:
    def test_auto_needs_a_pooled_polykv_root(self):
        backend = ContBackend()
        assert compaction.continues(_scope(backend), "trigger") == (
            False, "not under PolyKV")
        assert compaction.continues(
            _scope(Placed(backend, FakeManager(False))), "trigger")[0] is False
        assert compaction.continues(
            _scope(Placed(backend, FakeManager(True))), "trigger")[0] is True

    def test_on_continues_any_backend_off_none(self):
        backend = ContBackend()
        assert compaction.continues(_scope(backend, continuation="on"),
                                    "trigger")[0] is True
        assert compaction.continues(
            _scope(Placed(backend, FakeManager()), continuation="off"),
            "trigger")[0] is False

    def test_never_in_overflow_recovery(self):
        scope = _scope(ContBackend(), continuation="on")
        ok, why = compaction.continues(scope, "overflow")
        assert ok is False and "overflow" in why

    def test_a_failing_probe_is_no_continuation(self):
        class Broken(FakeManager):
            def continues(self, client, model):
                raise RuntimeError("probe")
        ok, _ = compaction.continues(
            _scope(Placed(ContBackend(), Broken())), "trigger")
        assert ok is False

    def test_off_sends_the_transcript_as_text(self):
        out, backend, mgr, _ = _run(continuation="off")
        assert out.changed
        assert all(p["messages"][0]["content"] in STEP_SYSTEMS
                   for p in backend.payloads)
        assert all(len(p["messages"]) == 2 for p in backend.payloads)
        assert mgr.erased == []
        assert out.report["continuation"] == "off"


# --- the writer --------------------------------------------------------- #

class TestWriter:
    def test_writer_continues_the_lanes_messages_with_its_tools(self):
        out, backend, _, msgs = _run()
        assert out.changed
        (w,) = by_step(backend, P.WRITER_SYSTEM_PROMPT)
        # The lane's stream unchanged, the instruction on its last tool
        # result: no new user query re-renders the loop.
        assert w["messages"][:-1] == [dict(m) for m in msgs[:-1]]
        assert w["tools"] == TOOLS
        assert w["think"] is False
        ask = w["messages"][-1]
        assert ask["role"] == "tool"
        assert ask["tool_call_id"] == msgs[-1]["tool_call_id"]
        assert ask["content"].startswith(msgs[-1]["content"].rstrip())
        assert f"\n\n{TAG} {P.WRITER_SYSTEM_PROMPT}\n\n" in ask["content"]
        assert TAG not in msgs[-1]["content"]          # never mutated
        # The transcript is above, never pasted again.
        assert "Conversation:" not in ask["content"]
        assert "The replay covers the conversation above" in ask["content"]
        assert "in the order they appear in the conversation above" in ask["content"]
        assert ("Do not continue the conversation above these instructions"
                in ask["content"])
        assert out.report["continuation"] == "PolyKV pooled"
        assert out.report["digest"]["continuation"] == "continuation"

    def test_the_lanes_options_and_wire_are_sent(self):
        seen = []

        def wire(m):
            seen.append(len(m))
            return [{**m[0], "content": m[0]["content"] + "\n# Output Budget"},
                    *m[1:]]
        out, backend, _, _ = _run(options={"num_ctx": 32768}, wire=wire)
        (w,) = by_step(backend, P.WRITER_SYSTEM_PROMPT)
        assert w["options"]["num_ctx"] == 32768
        assert w["options"]["num_predict"] > 1
        assert w["messages"][0]["content"].endswith("# Output Budget")
        assert seen

    def test_no_room_for_the_writer_is_sent_as_text(self):
        backend = ContBackend()
        mgr = FakeManager()
        client = Placed(backend, mgr)
        msgs = _loop_history()
        scope = _scope(client)
        plan = _plan(backend, msgs)
        # The lane's prompt alone overruns a window this size.
        digest = compaction.write_digest(
            scope, msgs[2:30], target_tokens=4000, context_tokens=2000,
            conversation=msgs)
        assert digest.info["continuation"].startswith("no room in the window")
        assert all(p["messages"][0]["content"] in STEP_SYSTEMS
                   for p in backend.payloads)
        assert plan is not None

    def test_a_writer_that_writes_nothing_falls_back_to_text(self):
        out, backend, mgr, _ = _run(backend=ContBackend(writer_text=""))
        cont = [p for p in backend.payloads
                if step_of(p) == P.WRITER_SYSTEM_PROMPT
                and p["messages"][0]["content"] != P.WRITER_SYSTEM_PROMPT]
        text = [p for p in backend.payloads
                if p["messages"][0]["content"] == P.WRITER_SYSTEM_PROMPT]
        assert len(cont) == compaction.DEFAULT_SETTINGS.writer_attempts
        assert text and "Conversation:" in text[0]["messages"][-1]["content"]
        assert "sent as text" in out.report["digest"]["continuation"]
        # What the continuation held is given back before the text path.
        assert mgr.erased == ["lane-sess"]


# --- the critics and the release ---------------------------------------- #

class TestCouncil:
    def test_critics_continue_after_the_writers_turn(self):
        out, backend, _, msgs = _run()
        critics = by_step(backend, P.CRITIC_SYSTEM_PROMPT)
        assert len(critics) == 2
        for c in critics:
            m = c["messages"]
            # The same stream the writer shared: the halves travel in the
            # request, never as turns that would re-render the loop.
            assert m[:-1] == [dict(x) for x in msgs[:-1]]
            assert m[-1]["role"] == "tool"
            assert f"{TAG} {P.CRITIC_SYSTEM_PROMPT}" in m[-1]["content"]
            assert compaction.CONTINUATION_SOURCE in m[-1]["content"]
            assert ("The transcript both halves were written from"
                    not in m[-1]["content"])
            assert "from the conversation above" in m[-1]["content"]
            assert c["tools"] == TOOLS

    def test_retrospective_and_synthesizer_are_requests_of_their_own(self):
        _, backend, _, _ = _run()
        for step in (P.RETROSPECTIVE_SYSTEM_PROMPT, P.SYNTHESIZER_SYSTEM_PROMPT):
            for p in by_step(backend, step):
                assert p["messages"][0]["content"] == step
                assert len(p["messages"]) == 2
                assert "tools" not in p

    def test_the_slot_is_given_back_after_the_critics_before_the_synthesizer(self):
        _, _, mgr, _ = _run()
        assert mgr.erased == ["lane-sess"]
        order = mgr.order
        erase = order.index("erase")
        assert max(i for i, s in enumerate(order)
                   if s == P.CRITIC_SYSTEM_PROMPT) < erase
        assert order.index(P.SYNTHESIZER_SYSTEM_PROMPT) > erase

    def test_reduced_flow_reviewer_continues_then_releases(self):
        _, backend, mgr, msgs = _run(flow="reduced")
        (r,) = by_step(backend, P.REVIEWER_SYSTEM_PROMPT)
        assert len(r["messages"]) == len(msgs)
        assert compaction.CONTINUATION_SOURCE in r["messages"][-1]["content"]
        assert "from the conversation above" in r["messages"][-1]["content"]
        assert mgr.erased == ["lane-sess"]
        assert mgr.order.index("erase") > mgr.order.index(P.REVIEWER_SYSTEM_PROMPT)

    def test_released_once_even_without_a_council(self):
        backend = ContBackend(writer_text="One paragraph, no marker. [#1]")
        _, _, mgr, _ = _run(backend=backend)
        assert mgr.erased == ["lane-sess"]

    def test_a_digest_that_raises_still_releases(self, monkeypatch):
        mgr = FakeManager()
        client = Placed(ContBackend(), mgr)
        msgs = _loop_history()
        scope = _scope(client)

        def boom(*a, **kw):
            scope.cont = compaction.Continuation(messages=[], reason="x",
                                                 session="lane-sess")
            raise RuntimeError("writer exploded")
        monkeypatch.setattr(compaction, "write_digest", boom)
        compaction.compact(scope, msgs, plan=_plan(client, msgs), tools=TOOLS)
        assert mgr.erased == ["lane-sess"]
        assert scope.cont is None


# --- the span ----------------------------------------------------------- #

class TestDescribeSpan:
    def test_names_the_first_folded_and_first_kept_message(self):
        msgs = _loop_history(n=6)
        text = compaction.describe_span(msgs, msgs[2:8])
        assert "from the message that begins" in text
        assert "up to, and not including, the message that begins" in text
        assert "step 3" in text             # msgs[8] is the call c3
        assert "do not call any tool" in text

    def test_to_the_end_without_a_tail_and_names_the_pinned_prompt(self):
        msgs = _loop_history(n=3) + [{"role": "user", "content": "now fix it"}]
        pinned = msgs[-1]
        text = compaction.describe_span(msgs, msgs[2:-1], pinned)
        assert "to its end" in text
        assert "now fix it»" in text

    def test_in_context_rewrites_only_the_position(self):
        assert ("conversation above" in P.in_context(P.CRITIC_PROMPT))
        assert "follows it" not in P.in_context(P.REVIEWER_PROMPT)
        assert P.in_context("plain") == "plain"


# --- the PolyKV side ---------------------------------------------------- #

class CloseRecorder:
    def __init__(self, found=True):
        self.closed = []
        self.found = found

    def close_session(self, sid, *, keep_kv=False, keep_running=False):
        self.closed.append((sid, keep_running))
        return type("R", (), {"found": self.found})()


class TestManager:
    def _mgr(self, mode="pooled"):
        mgr = polykv.KvRunManager("run-x", mode="pooled")
        root = polykv.Root(key="r", kind="xollama", client=CloseRecorder(),
                           mode=mode, settled=True)
        mgr.roots["r"] = root
        mgr.sessions["s1"] = "r"
        return mgr, root

    def test_erase_keeps_the_session_running(self):
        mgr, root = self._mgr()
        assert mgr.erase_session("s1") is True
        assert root.client.closed == [("s1", True)]
        assert mgr.events[-1]["kind"] == "erased"

    def test_erase_skips_a_busy_or_unknown_session(self):
        mgr, root = self._mgr()
        mgr._busy.add("s1")
        assert mgr.erase_session("s1") is False
        assert mgr.erase_session("nobody") is False
        assert root.client.closed == []

    @pytest.mark.parametrize("mode,want", [("pooled", True), ("assured", True),
                                           ("affinity", False), ("off", False)])
    def test_continues_only_where_a_layer_covers_the_head(self, mode, want,
                                                          monkeypatch):
        mgr, root = self._mgr(mode)
        monkeypatch.setattr(mgr, "_root", lambda c, m: root)
        monkeypatch.setattr(mgr, "_settle_mode", lambda r, m: None)
        assert mgr.continues(object(), "m") is want

    def test_a_released_manager_never_continues(self, monkeypatch):
        mgr, root = self._mgr()
        monkeypatch.setattr(mgr, "_root", lambda c, m: root)
        mgr.released = True
        assert mgr.continues(object(), "m") is False

    def test_last_placement_is_per_thread(self):
        class Inner:
            def chat(self, payload):
                return {"message": {"content": "x"}}

        class Mgr:
            def __init__(self):
                self.n = 0

            def place(self, client, payload, *, role):
                self.n += 1
                return {"session_id": f"s{payload['k']}"}

            def make_room(self, placement):
                return None

            def observe(self, *a, **kw):
                pass
        placed = polykv.PlacedChat(Inner(), Mgr(), role="researcher")
        placed.chat({"k": 1, "messages": []})
        seen = {}

        def other():
            seen["other"] = placed.last_placement()
            placed.chat({"k": 2, "messages": []})
            seen["after"] = placed.last_placement()
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert seen["other"] is None
        assert seen["after"]["session_id"] == "s2"
        assert placed.last_placement()["session_id"] == "s1"


def test_critics_never_continue_a_released_or_writerless_continuation():
    scope = _scope(ContBackend())
    assert compaction._continuing(scope) is None
    scope.cont = compaction.Continuation(messages=[], reason="x")
    assert compaction._continuing(scope) is None          # no writer turn
    scope.cont.writer_text = "replay"
    assert compaction._continuing(scope) is scope.cont
    scope.cont.released = True
    assert compaction._continuing(scope) is None          # slot given back


class TestWithInstruction:
    def test_rides_on_a_last_user_turn(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "q"}]
        out = compaction.with_instruction(msgs, "do X")
        assert out[-1] == {"role": "user", "content": f"q\n\n{TAG} do X"}
        assert msgs[-1]["content"] == "q"

    def test_after_an_assistant_turn_is_a_harness_turn(self):
        msgs = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"}]
        out = compaction.with_instruction(msgs, "do X")
        assert out[:2] == msgs
        assert out[-1]["role"] == "user" and out[-1]["_harness"] is True
        assert out[-1]["content"] == f"{TAG} do X"

    def test_a_tool_result_with_parts_is_not_rewritten(self):
        msgs = [{"role": "tool", "content": [{"type": "text", "text": "r"}]}]
        out = compaction.with_instruction(msgs, "do X")
        assert out[0] is msgs[0] and out[-1]["_harness"] is True


SHORT = ('ollama chat HTTP 400: {"error":"request (24304 tokens, 17436 of them '
         'shareable from pool 0) exceeds the available context size (6400 '
         'private tokens), try increasing it"}')


class TestWiden:
    """A stated private window the engine counted short is restated from
    its own count, once, and the root's later estimates learn the gap
    (live: a writer over code-heavy history was stated 6,400 for 6,868
    private tokens plus its output)."""

    def _mgr(self):
        mgr = polykv.KvRunManager("run-w", mode="pooled")
        root = polykv.Root(key="r", kind="xollama", client=None,
                           mode="pooled", settled=True, ctx_max=32768)
        mgr.roots["r"] = root
        return mgr, root

    def test_restates_from_the_engines_count_and_learns_the_scale(self):
        mgr, root = self._mgr()
        placement = {"session_id": "s", "num_ctx": 6400, "_layer_root": "r"}
        payload = {"options": {"num_predict": 2000}}
        assert mgr.widen(placement, RuntimeError(SHORT), payload) is True
        # 6868 private + 256 floor + 2000 out, aligned to 256.
        assert placement["num_ctx"] == 9216
        assert root.private_scale == pytest.approx(6868 / (6400 - 256 - 2000))
        assert mgr.events[-1]["kind"] == "widened"

    def test_capped_at_the_session_ceiling(self):
        mgr, root = self._mgr()
        root.ctx_max = 6400
        placement = {"session_id": "s", "num_ctx": 6400, "_layer_root": "r"}
        assert mgr.widen(placement, RuntimeError(SHORT),
                         {"options": {"num_predict": 2000}}) is False
        assert placement["num_ctx"] == 6400 and root.private_scale == 1.0

    def test_a_live_booking_that_ignored_the_ask_is_resized(self):
        # The writer continues the lane's session: its 6,400 booking holds,
        # and the 12,288 it stated was ignored (live, run2).
        mgr, root = self._mgr()
        resized = []

        class Client:
            def resize_session(self, sid, num_ctx):
                resized.append((sid, num_ctx))
                return {}
        root.client = Client()
        placement = {"session_id": "lane", "num_ctx": 12288, "_layer_root": "r"}
        assert mgr.widen(placement, RuntimeError(SHORT),
                         {"options": {"num_predict": 2000}}) is True
        assert resized == [("lane", 12288)]
        assert root.private_scale == 1.0       # the estimate was not short
        assert mgr.events[-1]["resized"] is True

    def test_other_errors_and_unwindowed_calls_are_left_alone(self):
        mgr, _ = self._mgr()
        assert mgr.widen({"num_ctx": 6400, "_layer_root": "r"},
                         RuntimeError("HTTP 400: bad request"), {}) is False
        assert mgr.widen({"_layer_root": "r"}, RuntimeError(SHORT), {}) is False

    def test_a_learned_scale_sizes_the_next_estimate(self):
        mgr, root = self._mgr()
        root.sizes["7"] = (1000, 3000)
        payload = {"model": "m", "options": {"num_predict": 100},
                   "messages": [{"role": "system", "content": "s"},
                                {"role": "user", "content": "x" * 3000}]}

        class C:
            pass
        mgr._wire_messages = lambda c, m, msgs: msgs
        before = mgr._private_window(root, "7", C(), payload)
        root.private_scale = 2.0
        assert mgr._private_window(root, "7", C(), payload) > before

    def test_placed_chat_resends_once_with_the_wider_window(self):
        sent = []

        class Inner:
            def chat(self, payload):
                sent.append(dict(payload["placement"]))
                if len(sent) == 1:
                    raise RuntimeError(SHORT)
                return {"message": {"content": "ok"}}

        mgr, _ = self._mgr()
        mgr.place = lambda c, p, role: {"session_id": "s", "pool_id": "0",
                                        "num_ctx": 6400, "_layer_root": "r"}
        mgr.make_room = lambda placement: None
        placed = polykv.PlacedChat(Inner(), mgr, role="researcher")
        placed.chat({"model": "m", "messages": [],
                     "options": {"num_predict": 2000}})
        assert [s["num_ctx"] for s in sent] == [6400, 9216]


class CallingBackend(ContBackend):
    """Answers each continuation's first turn with a tool call."""

    def __init__(self, *a, always=False, **kw):
        super().__init__(*a, **kw)
        self.always = always

    def chat(self, payload):
        step = step_of(payload)
        last = payload["messages"][-1]
        if (step != payload["messages"][0]["content"] and step != "lane"
                and (self.always or last["role"] != "tool"
                     or last["content"] != compaction.CONTINUATION_TOOLS_OFF)):
            self.payloads.append(payload)
            return {"message": {"content": "", "tool_calls": [
                {"id": "x1", "function": {"name": "read_file",
                                          "arguments": {"path": "a.py"}}}]}}
        return super().chat(payload)


class TestToolCallInAContinuation:
    def test_a_call_is_answered_as_not_run_and_the_turn_asked_again(self):
        backend = CallingBackend()
        out, backend, mgr, msgs = _run(backend=backend)
        assert out.report["digest"]["continuation"] == "continuation"
        writers = by_step(backend, P.WRITER_SYSTEM_PROMPT)
        assert len(writers) == 2
        again = writers[1]["messages"]
        # The first ask unchanged, then the call and its answer appended:
        # still the lane's stream, no new user query.
        assert again[:len(msgs)] == writers[0]["messages"]
        assert again[-2]["role"] == "assistant" and again[-2]["tool_calls"]
        assert again[-1] == {"role": "tool", "tool_call_id": "x1",
                             "content": compaction.CONTINUATION_TOOLS_OFF}

    def test_only_once_per_call(self):
        out, backend, _, _ = _run(backend=CallingBackend(always=True))
        writers = [p for p in by_step(backend, P.WRITER_SYSTEM_PROMPT)
                   if p["messages"][0]["content"] != P.WRITER_SYSTEM_PROMPT]
        # Two sends per attempt, then the text path takes the fold.
        assert len(writers) == 2 * compaction.DEFAULT_SETTINGS.writer_attempts
        assert "sent as text" in out.report["digest"]["continuation"]


def test_continuation_is_off_by_default():
    # compaction-v7-cont2 (2026-10-24): continuing kept 26% of the carried
    # rows against 97% as text; the owner turned it off by default.
    from consultants.engine import compaction
    assert compaction.Settings().continuation == "off"
