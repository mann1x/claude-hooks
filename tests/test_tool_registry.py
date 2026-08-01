"""M-A + M-C: the composable tool registry, its permission gate, and the
git history provider.

See ``docs/PLAN-council-tool-surface.md``. Two properties dominate these
tests, because both are the kind that look fine until the day they
matter:

1. **The gate is the only path.** Every tool call goes through
   ``ToolRegistry.dispatch``. If a provider could be reached without it,
   the ladder would be advisory — so several tests here assert on
   routing rather than on behaviour.
2. **Everything fails closed.** An invalid level, a missing approval
   channel, a provider that raises: each must refuse rather than run.
   The alternative is that a config typo silently ungates a tool.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.tool_registry import (  # noqa: E402
    ASK_ASSISTANT,
    ASK_HUMAN,
    AUTO,
    DENY,
    BuiltinToolProvider,
    GateDecision,
    GitToolProvider,
    PolicyGate,
    ToolProvider,
    ToolRegistry,
    escalate,
    is_valid_level,
)
from claude_hooks.tool_registry.base import namespaced  # noqa: E402


# ===================================================================== #
# Doubles
# ===================================================================== #
class FakeProvider(ToolProvider):
    def __init__(self, name="fake", prefix="", tools=("alpha",),
                 read_only=True, level=AUTO, taints=False,
                 specs_raise=False, exec_raise=False):
        self.name = name
        self.prefix = prefix
        self._tools = tuple(tools)
        self._read_only = read_only
        self._level = level
        self._taints = taints
        self._specs_raise = specs_raise
        self._exec_raise = exec_raise
        self.calls: list[tuple[str, str, str]] = []

    def specs(self):
        if self._specs_raise:
            raise RuntimeError("provider is broken")
        return [namespaced(self.prefix, {
            "type": "function",
            "function": {"name": t, "description": t, "parameters": {}},
        }) for t in self._tools]

    def execute(self, tool, raw_args, cwd):
        if self._exec_raise:
            raise RuntimeError("boom")
        self.calls.append((tool, raw_args, cwd))
        return f"ran {tool}"

    def default_level(self, tool):
        return self._level

    def is_read_only(self, tool):
        return self._read_only

    def taints(self):
        return self._taints


# ===================================================================== #
# The ladder
# ===================================================================== #
class TestLadder(unittest.TestCase):
    def test_escalates_one_rung(self):
        self.assertEqual(escalate(AUTO), ASK_ASSISTANT)
        self.assertEqual(escalate(ASK_ASSISTANT), ASK_HUMAN)

    def test_ask_human_is_the_ceiling(self):
        """Escalating ask_human into deny would convert 'ask a person'
        into 'refuse' — a different decision than the one configured."""
        self.assertEqual(escalate(ASK_HUMAN), ASK_HUMAN)

    def test_deny_is_terminal(self):
        self.assertEqual(escalate(DENY), DENY)

    def test_unknown_level_escalates_to_deny(self):
        self.assertEqual(escalate("banana"), DENY)

    def test_level_validation(self):
        self.assertTrue(is_valid_level(AUTO))
        self.assertFalse(is_valid_level("ask"))      # the old 3-value enum
        self.assertFalse(is_valid_level(None))
        self.assertFalse(is_valid_level(1))


# ===================================================================== #
# Gate resolution
# ===================================================================== #
class TestGateResolution(unittest.TestCase):
    def test_default_when_nothing_matches(self):
        self.assertEqual(PolicyGate().resolve("x").level, AUTO)

    def test_provider_default_beats_gate_default(self):
        g = PolicyGate(provider_defaults={"shell": ASK_ASSISTANT})
        self.assertEqual(g.resolve("shell").level, ASK_ASSISTANT)

    def test_config_override_beats_provider_default(self):
        g = PolicyGate(overrides={"shell": ASK_HUMAN},
                       provider_defaults={"shell": ASK_ASSISTANT})
        self.assertEqual(g.resolve("shell").level, ASK_HUMAN)

    def test_runtime_control_beats_config(self):
        """The live HTTP control surface must be able to tighten a
        running council without a restart."""
        g = PolicyGate(overrides={"shell": AUTO})
        d = g.resolve("shell", runtime_permissions={"shell": DENY})
        self.assertEqual(d.level, DENY)

    def test_invalid_level_fails_closed(self):
        """A typo in config must never produce an ungated tool."""
        g = PolicyGate(overrides={"shell": "allow"})   # pre-ladder value
        d = g.resolve("shell")
        self.assertEqual(d.level, DENY)
        self.assertIn("invalid permission level", d.reason)

    def test_reason_names_the_tool_and_the_source(self):
        g = PolicyGate(overrides={"shell": ASK_HUMAN})
        self.assertIn("shell", g.resolve("shell").reason)
        self.assertIn("config", g.resolve("shell").reason)


# ===================================================================== #
# Taint
# ===================================================================== #
class TestTaint(unittest.TestCase):
    def test_effectful_tool_escalates_when_tainted(self):
        g = PolicyGate(provider_defaults={"shell": AUTO})
        g.taint("web fetch")
        d = g.resolve("shell")
        self.assertEqual(d.level, ASK_ASSISTANT)
        self.assertTrue(d.escalated_by_taint)

    def test_read_only_tool_is_never_escalated(self):
        """Read-only tools are why ``auto`` is cheap; escalating them
        would cost tokens on every grep and buy no safety."""
        g = PolicyGate(read_only_tools=frozenset({"grep"}))
        g.taint()
        d = g.resolve("grep")
        self.assertEqual(d.level, AUTO)
        self.assertFalse(d.escalated_by_taint)

    def test_taint_overrides_an_operator_pin(self):
        """The taint rule is a safety backstop, not a preference, so it
        applies on top of an explicit auto."""
        g = PolicyGate(overrides={"shell": AUTO})
        g.taint()
        self.assertEqual(g.resolve("shell").level, ASK_ASSISTANT)

    def test_deny_stays_denied_when_tainted(self):
        g = PolicyGate(overrides={"shell": DENY})
        g.taint()
        self.assertEqual(g.resolve("shell").level, DENY)

    def test_ask_human_does_not_move(self):
        g = PolicyGate(overrides={"pod": ASK_HUMAN})
        g.taint()
        self.assertEqual(g.resolve("pod").level, ASK_HUMAN)

    def test_taint_is_sticky(self):
        """There is no point at which ingested untrusted text leaves the
        context, so an untaint would be a lie."""
        g = PolicyGate()
        g.taint()
        g.taint()
        self.assertTrue(g.tainted)
        self.assertFalse(hasattr(g, "untaint"))

    def test_reason_explains_the_escalation(self):
        g = PolicyGate(provider_defaults={"shell": AUTO})
        g.taint()
        self.assertIn("untrusted external content",
                      g.resolve("shell").reason)


# ===================================================================== #
# Registry composition
# ===================================================================== #
class TestRegistryComposition(unittest.TestCase):
    def test_merges_across_providers(self):
        r = ToolRegistry([FakeProvider(tools=("a",)),
                          FakeProvider(name="p2", tools=("b",))])
        self.assertEqual(r.tool_names(), ["a", "b"])

    def test_first_provider_wins_a_collision(self):
        """The schema advertised and the implementation executed must
        come from the *same* provider.

        Regression: ``specs()`` kept the first provider's schema while
        ``_seed_gate`` claimed ownership unconditionally, so the last
        provider ran. The model would have seen one tool's description
        and silently called another's code — and with differing
        ``read_only`` marks it would also have been gated by the wrong
        policy.
        """
        p1 = FakeProvider(name="first", tools=("dup",))
        p2 = FakeProvider(name="second", tools=("dup",))
        r = ToolRegistry([p1, p2])
        self.assertEqual(r.tool_names(), ["dup"])
        r.dispatch("dup", "{}", "/tmp")
        self.assertTrue(p1.calls, "first provider must execute")
        self.assertFalse(p2.calls, "shadowed provider must not execute")

    def test_collision_uses_the_winner_s_policy(self):
        """The losing provider's read_only mark must not leak into the
        gate, or a tool could be advertised by one provider and exempted
        from taint by another."""
        p1 = FakeProvider(name="first", tools=("dup",), read_only=False)
        p2 = FakeProvider(name="second", tools=("dup",), read_only=True)
        r = ToolRegistry([p1, p2])
        r.gate.taint()
        self.assertTrue(r.gate.resolve("dup").escalated_by_taint)

    def test_prefix_namespacing(self):
        r = ToolRegistry([FakeProvider(prefix="srv", tools=("search",))])
        self.assertEqual(r.tool_names(), ["srv__search"])

    def test_namespaced_does_not_mutate_the_input(self):
        """Providers routinely return cached or module-level dicts."""
        spec = {"type": "function", "function": {"name": "x"}}
        namespaced("p", spec)
        self.assertEqual(spec["function"]["name"], "x")

    def test_provider_that_raises_offers_no_tools(self):
        """One broken provider must not take down a council that would
        have worked without it."""
        good = FakeProvider(name="good", tools=("ok",))
        r = ToolRegistry([FakeProvider(name="bad", specs_raise=True), good])
        self.assertEqual(r.tool_names(), ["ok"])

    def test_unknown_tool_is_an_error_string(self):
        r = ToolRegistry([FakeProvider()])
        self.assertIn("unknown tool", r.dispatch("nope", "{}", "/tmp"))

    def test_executor_adapter_matches_the_agent_loop_signature(self):
        r = ToolRegistry([FakeProvider(tools=("alpha",))])
        ex = r.as_executor()
        self.assertEqual(ex("alpha", "{}", "/tmp"), "ran alpha")


# ===================================================================== #
# Dispatch gating — the property that makes the ladder real
# ===================================================================== #
class TestDispatchGating(unittest.TestCase):
    def test_auto_runs_without_an_approval_channel(self):
        p = FakeProvider(tools=("alpha",))
        r = ToolRegistry([p])
        self.assertEqual(r.dispatch("alpha", "{}", "/tmp"), "ran alpha")

    def test_denied_tool_never_reaches_the_provider(self):
        p = FakeProvider(tools=("alpha",))
        r = ToolRegistry([p], gate=PolicyGate(overrides={"alpha": DENY}))
        out = r.dispatch("alpha", "{}", "/tmp")
        self.assertIn("not permitted", out)
        self.assertEqual(p.calls, [], "denied tool must not execute")

    def test_denial_is_a_tool_result_not_an_exception(self):
        """A denial that killed the lane would turn a policy decision
        into an outage; the model must see it and reroute."""
        r = ToolRegistry([FakeProvider()],
                         gate=PolicyGate(overrides={"alpha": DENY}))
        self.assertTrue(r.dispatch("alpha", "{}", "/tmp").startswith("error:"))

    def test_approval_granted_runs_the_tool(self):
        p = FakeProvider(tools=("alpha",))
        seen = []

        def approve(d):
            seen.append(d)
            return True

        r = ToolRegistry([p], gate=PolicyGate(overrides={"alpha": ASK_HUMAN}),
                         approval_fn=approve)
        self.assertEqual(r.dispatch("alpha", "{}", "/tmp"), "ran alpha")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], GateDecision)
        self.assertEqual(seen[0].level, ASK_HUMAN)

    def test_approval_refused_blocks_the_tool(self):
        p = FakeProvider(tools=("alpha",))
        r = ToolRegistry([p], gate=PolicyGate(overrides={"alpha": ASK_HUMAN}),
                         approval_fn=lambda d: False)
        self.assertIn("not approved", r.dispatch("alpha", "{}", "/tmp"))
        self.assertEqual(p.calls, [])

    def test_no_approval_channel_refuses(self):
        """/get-advice and caliber have no channel. Running an
        ask_human action because nobody was listening is exactly the
        failure the ladder exists to prevent."""
        p = FakeProvider(tools=("alpha",))
        r = ToolRegistry([p], gate=PolicyGate(overrides={"alpha": ASK_HUMAN}))
        self.assertIn("not approved", r.dispatch("alpha", "{}", "/tmp"))
        self.assertEqual(p.calls, [])

    def test_approval_channel_that_raises_refuses(self):
        p = FakeProvider(tools=("alpha",))

        def boom(d):
            raise RuntimeError("channel down")

        r = ToolRegistry([p], gate=PolicyGate(overrides={"alpha": ASK_HUMAN}),
                         approval_fn=boom)
        self.assertIn("not approved", r.dispatch("alpha", "{}", "/tmp"))
        self.assertEqual(p.calls, [])

    def test_decision_carries_args_for_the_approver(self):
        """An approver cannot judge a call without seeing its args."""
        seen = []
        r = ToolRegistry([FakeProvider()],
                         gate=PolicyGate(overrides={"alpha": ASK_ASSISTANT}),
                         approval_fn=lambda d: seen.append(d) or True)
        r.dispatch("alpha", '{"cmd":"pytest"}', "/tmp")
        self.assertIn("pytest", seen[0].raw_args)
        self.assertIn("pytest", json.dumps(seen[0].to_payload()))

    def test_provider_exception_becomes_an_error_string(self):
        r = ToolRegistry([FakeProvider(exec_raise=True)])
        self.assertTrue(
            r.dispatch("alpha", "{}", "/tmp").startswith("error:"))

    def test_effectful_provider_is_gated_once_tainted(self):
        p = FakeProvider(tools=("mutate",), read_only=False)
        r = ToolRegistry([p])
        r.gate.taint("web result")
        self.assertIn("not approved", r.dispatch("mutate", "{}", "/tmp"))

    def test_provider_that_taints_on_activation(self):
        """An unvalidated MCP server taints merely by being listed: its
        tool descriptions are third-party text the model reads whether
        or not the tool is called."""
        r = ToolRegistry([FakeProvider(name="mcp", taints=True)])
        self.assertTrue(r.gate.tainted)


# ===================================================================== #
# Builtin provider
# ===================================================================== #
class TestBuiltinProvider(unittest.TestCase):
    def test_offers_the_six_and_keeps_bare_names(self):
        """Renaming these would invalidate the M11c benchmark corpus."""
        names = [s["function"]["name"] for s in BuiltinToolProvider().specs()]
        self.assertEqual(
            names,
            ["survey_project", "list_files", "read_file", "glob", "grep",
             "recall_memory"])

    def test_all_read_only_and_none_taint(self):
        p = BuiltinToolProvider()
        self.assertTrue(all(p.is_read_only(n) for n in ("read_file", "grep")))
        self.assertFalse(p.taints())

    def test_defaults_to_auto(self):
        self.assertEqual(BuiltinToolProvider().default_level("grep"), AUTO)

    def test_matches_the_registry_the_council_uses_today(self):
        """M12 parity guard: the default surface must not drift from
        what ``openai_tool_specs()`` returns, or every existing
        consultants fixture changes meaning."""
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
        self.assertEqual(BuiltinToolProvider().specs(), openai_tool_specs())

    def test_executes_through_the_registry(self):
        r = ToolRegistry([BuiltinToolProvider()])
        out = r.dispatch("grep", json.dumps(
            {"pattern": "DEFAULT_MAX_QUERY_CHARS", "path": "claude_hooks"}),
            str(REPO))
        self.assertIn("recall.py", out)


# ===================================================================== #
# Git provider (M-C)
# ===================================================================== #
def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


@unittest.skipUnless(_git_available(), "git not installed")
class TestGitProvider(unittest.TestCase):
    def setUp(self):
        self.p = GitToolProvider()
        self.cwd = str(REPO)

    def _run(self, tool, **args):
        return self.p.execute(tool, json.dumps(args), self.cwd)

    # -- the composed tool, which is the point of the milestone ------- #
    def test_history_finds_the_commit_that_added_a_symbol(self):
        out = self._run("git_history", path="claude_hooks/recall.py",
                        symbol="clamp_query", max_count=3)
        self.assertNotIn("error:", out.splitlines()[0])
        self.assertIn("commit", out)

    def test_history_by_line_range_when_no_symbol(self):
        out = self._run("git_history", path="claude_hooks/recall.py",
                        start_line=1, end_line=5, max_count=2)
        self.assertIn("commit", out)

    def test_history_rejects_a_colon_in_the_symbol(self):
        """':' terminates git's -L funcname form."""
        self.assertIn("error:", self._run(
            "git_history", path="README.md", symbol="a:b"))

    # -- primitives ---------------------------------------------------- #
    def test_log_returns_commits(self):
        self.assertIn("—", self._run("git_log", max_count=3))

    def test_log_follows_a_path(self):
        out = self._run("git_log", path="claude_hooks/recall.py", max_count=3)
        self.assertNotIn("error:", out)

    def test_blame_accepts_a_line_range(self):
        out = self._run("git_blame", path="README.md",
                        start_line=1, end_line=3)
        self.assertNotIn("error:", out.splitlines()[0])

    def test_blame_requires_a_path(self):
        self.assertIn("path is required", self._run("git_blame"))

    def test_diff_stat_only(self):
        self.assertNotIn("error:", self._run(
            "git_diff", from_rev="HEAD~1", to_rev="HEAD", stat_only=True))

    def test_show_requires_a_rev(self):
        self.assertIn("error:", self._run("git_show"))

    # -- confinement + injection surface ------------------------------- #
    def test_path_outside_roots_is_refused(self):
        self.assertIn("outside the allowed roots",
                      self._run("git_blame", path="/etc/hostname"))

    def test_rev_starting_with_dash_is_refused(self):
        """Revisions land in argv, so there is no shell to inject — but
        git would still read a leading-dash value as a flag."""
        self.assertIn("error:", self._run("git_show", rev="--output=/tmp/x"))

    def test_only_observation_subcommands_are_reachable(self):
        """No code path here can reach a writing subcommand."""
        from claude_hooks.tool_registry import git_provider as gp
        for bad in ("commit", "checkout", "gc", "push", "reset", "clean"):
            self.assertNotIn(bad, gp._ALLOWED_SUBCOMMANDS)
        self.assertIn("error:", gp._run_git(["commit", "-m", "x"], self.cwd))

    def test_unknown_tool(self):
        self.assertIn("unknown tool", self._run("git_nuke"))

    def test_bad_json_is_an_error_string(self):
        self.assertIn("not valid JSON",
                      self.p.execute("git_log", "{oops", self.cwd))

    def test_non_object_args(self):
        self.assertIn("must be a JSON object",
                      self.p.execute("git_log", "[1,2]", self.cwd))

    def test_output_is_capped(self):
        """git log -p over a long-lived file is megabytes; an unbounded
        result would blow the context rather than answer anything."""
        from claude_hooks.tool_registry import git_provider as gp
        out = self._run("git_diff", from_rev="HEAD~40", to_rev="HEAD")
        self.assertLessEqual(len(out), gp.MAX_OUTPUT_CHARS + len(gp._TRUNC))

    def test_not_a_repo_is_explained(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = GitToolProvider().execute("git_log", "{}", d)
            self.assertIn("not a git repository", out)

    def test_all_read_only_none_taint(self):
        self.assertTrue(self.p.is_read_only("git_log"))
        self.assertFalse(self.p.taints())
        self.assertEqual(self.p.default_level("git_log"), AUTO)

    def test_reachable_through_the_registry(self):
        r = ToolRegistry([BuiltinToolProvider(), GitToolProvider()])
        self.assertIn("git_history", r.tool_names())
        self.assertIn("commit", r.dispatch(
            "git_history",
            json.dumps({"path": "claude_hooks/recall.py",
                        "symbol": "clamp_query", "max_count": 2}),
            str(REPO)))


class TestGitRootConfinement(unittest.TestCase):
    def test_extra_roots_are_honoured(self):
        from claude_hooks.tool_registry.git_provider import _resolve_in_roots
        self.assertIsNotNone(
            _resolve_in_roots("x.py", "/tmp/a", ("/tmp/b",)))
        self.assertIsNotNone(
            _resolve_in_roots("/tmp/b/x.py", "/tmp/a", ("/tmp/b",)))
        self.assertIsNone(
            _resolve_in_roots("/tmp/c/x.py", "/tmp/a", ("/tmp/b",)))

    def test_traversal_is_refused(self):
        from claude_hooks.tool_registry.git_provider import _resolve_in_roots
        self.assertIsNone(
            _resolve_in_roots("../../etc/passwd", "/tmp/a", ()))

    @unittest.skipIf(os.name == "nt", "POSIX path semantics")
    def test_sibling_prefix_is_not_inside_the_root(self):
        """``/tmp/aaa`` must not count as inside ``/tmp/a``."""
        from claude_hooks.tool_registry.git_provider import _resolve_in_roots
        self.assertIsNone(_resolve_in_roots("/tmp/aaa/x", "/tmp/a", ()))


if __name__ == "__main__":
    unittest.main()
