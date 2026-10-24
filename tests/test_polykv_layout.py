"""The shared prompt layout on PolyKV roots (plan F4).

What the roles have in common goes first and each role's own text last,
so the critic, synthesizer and the rest share the question, plan and
reports; the manager stacks a forked layer per shared block and attaches
the deepest. Off a PolyKV root (affinity, off, as-built) nothing changes.
"""

from __future__ import annotations

from consultants.engine import council, polykv
from consultants.engine import prompt_layout as PL
from consultants.engine.budget import with_output_budget_section
from tests.test_polykv_manager import FakeChat, PoolEngine, SizedEngine, _mgr

QUESTION = "Why does the retry loop give up after one attempt?"
PLAN = "1. Read retry.py.\n2. Check the caller in client.py."
REPORTS = ["retry.py:40 returns early when status is 429.\n\n" + "a" * 3000,
           "client.py:12 never passes max_attempts.\n\n" + "b" * 3000,
           "the tests pin one attempt.\n\n" + "c" * 3000]
GROUNDING = [{"role": "system", "content": "GROUNDING " + "g" * 2500}]


def critic():
    return council.build_critic_messages(QUESTION, PLAN, REPORTS)


def synth(critique=None):
    return council.build_synthesizer_messages(QUESTION, PLAN, REPORTS,
                                              critique)


def researcher():
    msgs = council.build_researcher_messages(QUESTION, "1. Read retry.py.",
                                             [], GROUNDING)
    return with_output_budget_section(msgs, 8000, 32768)


# ----------------------------------------------------------- the layout

class TestRoleLast:

    def test_the_system_turn_is_the_shared_preamble(self):
        for build in (critic, synth, researcher):
            out = PL.role_last(build())
            assert out[0] == {"role": "system",
                              "content": PL.shared_system()}, build.__name__

    def test_the_role_text_goes_to_the_end_of_the_first_user_turn(self):
        before = critic()
        out = PL.role_last(before)
        user = out[-1]["content"]
        role_text = before[0]["content"][len(council.COUNCIL_PREAMBLE):].strip()
        assert user.startswith(before[1]["content"].rstrip())
        assert user.endswith(PL.ROLE_HEADING + "\n\n" + role_text)

    def test_grounding_stays_and_the_budget_moves_with_the_role(self):
        before = researcher()
        assert "# Output Budget" in before[0]["content"]   # on the grounding
        out = PL.role_last(before)
        assert out[1]["content"] == GROUNDING[0]["content"]
        assert "# Output Budget" not in out[1]["content"]
        assert "# Output Budget" in out[2]["content"]
        assert out[2]["content"].index(PL.ROLE_HEADING) \
            < out[2]["content"].index("# Output Budget")

    def test_critic_and_synthesizer_share_question_plan_and_reports(self):
        c = PL.role_last(critic())[1]["content"]
        s = PL.role_last(synth())[1]["content"]
        shared = PL.common_prefix(c, s)
        last = c.rfind("RESEARCHER REPORT 3")
        assert shared > last + len(REPORTS[2])

    def test_the_tool_loop_tail_is_kept(self):
        msgs = researcher() + [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file",
                                          "arguments": {"path": "x"}}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "body"}]
        out = PL.role_last(msgs)
        assert out[-2:] == msgs[-2:]

    def test_once_is_enough(self):
        once = PL.role_last(critic())
        assert PL.role_last(once) is once

    def test_other_conversations_are_left_alone(self):
        from consultants.engine import compaction_prompts as CP
        for msgs in ([{"role": "system", "content": CP.WRITER_SYSTEM_PROMPT},
                      {"role": "user", "content": "transcript"}],
                     [{"role": "user", "content": "no system"}],
                     [{"role": "system", "content": council.COUNCIL_PREAMBLE}],
                     []):
            assert PL.role_last(msgs) is msgs


class TestCuts:

    def test_question_plan_and_all_but_the_last_report(self):
        text = critic()[1]["content"]
        cuts = PL.block_cuts(text)
        assert text[:cuts[0]] == f"USER QUESTION:\n{QUESTION}"
        assert text[cuts[1]:].startswith("\n\nRESEARCHER REPORT 1")
        assert text[cuts[2]:].startswith("\n\nRESEARCHER REPORT 3")
        assert len(cuts) == 3

    def test_a_turn_not_opening_with_the_question_has_none(self):
        assert PL.block_cuts("QUESTION\nlane 1") == []
        assert PL.block_cuts("USER QUESTION:\nonly the question") == []
        assert PL.block_cuts("notes\n\nPLANNER'S PLAN:\nx\n\n"
                             "RESEARCHER REPORT 1:\ny") == []

    def test_snap_ends_on_a_paragraph(self):
        text = "one two\n\nthree four\n\nfive"
        assert PL.snap(text, len(text)) == len("one two\n\nthree four")
        assert PL.snap(text, 12) == len("one two")
        assert PL.snap("no paragraphs here", 10) == 0

    def test_common_prefix(self):
        a = "x" * 10000 + "AB"
        assert PL.common_prefix(a, "x" * 10000 + "AC") == 10001
        assert PL.common_prefix("", "a") == 0


# ----------------------------------------------------------- the manager

def _placed(mgr, chat, role):
    return polykv.PlacedChat(chat, mgr, role=role)


def _send(mgr, chat, role, msgs):
    _placed(mgr, chat, role).chat({"model": "q", "messages": msgs,
                                   "options": {"num_predict": 500}})
    return chat.payloads[-1]


def test_pooled_calls_go_out_in_the_shared_layout():
    eng, chat = PoolEngine(), FakeChat()
    sent = _send(_mgr(eng), chat, "critic", critic())
    assert sent["messages"][0]["content"] == PL.shared_system()
    assert PL.ROLE_HEADING in sent["messages"][1]["content"]


def test_as_built_affinity_and_off_send_what_the_builders_wrote():
    for kw in ({"layout": "as-built"}, {"mode": "affinity"}, {"mode": "off"}):
        eng, chat = PoolEngine(), FakeChat()
        msgs = critic()
        sent = _send(_mgr(eng, **kw), chat, "critic", msgs)
        assert sent["messages"] == msgs, kw


def test_judges_attach_one_chain_of_forked_layers():
    eng, chat = PoolEngine(), FakeChat()
    mgr = _mgr(eng)
    a = _send(mgr, chat, "critic", critic())["placement"]["pool_id"]
    # The system run, the question and the plan are each under the layer
    # minimum, so the first layer ends before the last report.
    first = eng.pools[int(a)]["body"]["prompt"]
    assert first.endswith(REPORTS[1])
    b = _send(mgr, chat, "synthesizer", synth())["placement"]["pool_id"]
    # The synthesizer reaches past the last report header: one deeper fork
    # of the critic's chain, ending where the two prompts part.
    assert eng.pools[int(b)]["parent"] is not None
    chain, p = [], int(b)
    while p is not None:
        chain.append(p)
        p = eng.pools[p]["parent"]
    assert int(a) in chain
    deepest = eng.pools[int(b)]["body"]["prompt"]
    assert deepest.endswith(REPORTS[2])


def test_lanes_of_one_tool_loop_build_nothing_new_per_turn():
    eng, chat = PoolEngine(), FakeChat()
    mgr = _mgr(eng)
    msgs = critic()
    _send(mgr, chat, "critic", msgs)
    made = len(eng.pools)
    renders = sum(1 for r in eng.requests if r[1] == "apply-template")
    for i in range(3):
        msgs = msgs + [{"role": "assistant", "content": f"turn {i}"},
                       {"role": "user", "content": "go on"}]
        _send(mgr, chat, "critic", msgs)
    assert len(eng.pools) == made
    # Same first user turn, same chain: nothing is rendered again.
    assert sum(1 for r in eng.requests if r[1] == "apply-template") == renders


def test_the_private_window_leaves_out_what_the_layer_covers():
    eng, chat = SizedEngine(), FakeChat()
    mgr = _mgr(eng)
    sent = _send(mgr, chat, "critic", critic())
    pid = sent["placement"]["pool_id"]
    content = sent["messages"][1]["content"]
    assert mgr._covered[str(pid)] == content.rfind("\n\nRESEARCHER REPORT 3")
    # The window is what the layer leaves: the last report and the role
    # text (~3,800 chars at 2 per cell, x1.3) plus 500 out, not the
    # reports again.
    assert sent["placement"]["num_ctx"] <= 4096
    mgr._covered.clear()
    again = _send(mgr, chat, "critic", critic())["placement"]["num_ctx"]
    assert again > sent["placement"]["num_ctx"] + 1024


def test_release_frees_children_before_parents():
    eng, chat = PoolEngine(), FakeChat()
    mgr = _mgr(eng)
    _send(mgr, chat, "critic", critic())
    _send(mgr, chat, "synthesizer", synth())
    report = mgr.release_all()
    assert report["close_errors"] == []
    assert eng.pools == {}


def test_a_refused_fork_shares_what_it_already_has():
    eng, chat = PoolEngine(), FakeChat()
    orig = eng.handle

    def no_fork(method, route, params, data):
        if route.endswith("/fork"):
            eng.requests.append((method, route, params, data))
            from claude_hooks.polykv.client import Reply
            return Reply(400, {"error": {"message": "child prefix mismatch"}})
        return orig(method, route, params, data)
    eng.handle = no_fork
    mgr = _mgr(eng)
    a = _send(mgr, chat, "critic", critic())["placement"]["pool_id"]
    sent = _send(mgr, chat, "synthesizer", synth())
    # The deeper fork is refused: the call attaches the layer it has.
    assert sent["placement"]["pool_id"] == a
    assert any(e["kind"] == "layer_refused" for e in mgr.events)


def test_a_refused_block_does_not_stop_a_deeper_one():
    eng, chat = PoolEngine(), FakeChat()
    orig = eng.handle

    def refuse_first(method, route, params, data):
        if (route == "polykv/pools" and method == "POST"
                and str((data or {}).get("prompt", "")).endswith(REPORTS[1])):
            from claude_hooks.polykv.client import Reply
            return Reply(400, {"error": {"message": "no"}})
        return orig(method, route, params, data)
    eng.handle = refuse_first
    mgr = _mgr(eng)
    _send(mgr, chat, "critic", critic())
    sent = _send(mgr, chat, "synthesizer", synth())
    pid = sent["placement"]["pool_id"]
    assert pid is not None
    assert eng.pools[int(pid)]["body"]["prompt"].endswith(REPORTS[2])


def test_config_layout_round_trip(tmp_path):
    from consultants import config as cc
    cfg = cc.set_polykv(enabled=True, layout="as-built", scope="cwd",
                        cwd=tmp_path)
    assert cfg.polykv.layout == "as-built"
    try:
        cc.set_polykv(layout="sideways", scope="cwd", cwd=tmp_path)
    except ValueError as e:
        assert "layout" in str(e)
    else:
        raise AssertionError("a bad layout was accepted")


class _SizedForks(SizedEngine):
    """Forks answer their sizes, with a 32k per-session ceiling."""

    def handle(self, method, route, params, data):
        from claude_hooks.polykv.client import Reply
        if route == "props" and self.refuse is None:
            self.requests.append((method, route, params, data))
            return Reply(200, {"features": self.features, "opencoti": {
                "polykv": {"pools_enabled": True},
                "kv": {"session_ctx_max": 32768}}})
        out = super().handle(method, route, params, data)
        if route.endswith("/fork") and out.status == 200:
            cid = out.body["pool_id"]
            parent = self.pools[out.body["parent"]]
            own = len(data.get("prompt") or "") // 2
            self.pools[cid]["own_len"] = own
            self.pools[cid]["prefix_len"] = (parent.get("prefix_len")
                                             or parent.get("own_len", 0)) + own
            out = Reply(200, dict(self.pools[cid]))
        return out


def test_a_fork_attach_leaves_room_for_the_whole_chain():
    eng, chat = _SizedForks(), FakeChat()
    mgr = _mgr(eng)
    _send(mgr, chat, "critic", critic())
    sent = _send(mgr, chat, "synthesizer", synth())
    pid = str(sent["placement"]["pool_id"])
    chain = mgr.roots["direct|e"].chain_cells[pid]
    assert chain == eng.pools[int(pid)]["prefix_len"]
    assert chain > eng.pools[int(pid)]["own_len"]       # the parents count
    # A 96k cold-budget output would ask the ceiling: it gets what is left.
    big = synth()
    p = {"model": "q", "messages": big, "options": {"num_predict": 96000}}
    polykv.PlacedChat(chat, mgr, role="synthesizer").chat(p)
    placed = chat.payloads[-1]["placement"]
    assert placed["num_ctx"] + mgr.roots["direct|e"].chain_cells[
        str(placed["pool_id"])] <= 32768
