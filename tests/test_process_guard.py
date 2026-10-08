"""The process guard rejects commands that kill or wait on themselves,
and waiters that cannot notice a failure — and nothing else.

Every "fault" case below is a shape that happened: measured on bs2
(bash 5.2) on 2026-09-29 with a pattern that matched nothing else, or
taken from the buglogs / cerebrum files / transcripts where it cost a
session its shell, its connection or hours of an unnoticed dead job.
Every "clean" case is one the guard must leave alone, most of them
false positives found by running the guard over 22 000 recorded
commands, where a real self-kill always shows as exit 144 / 255 and
output that stops at the kill.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import process_guard as pg  # noqa: E402

ANCESTORS = [("claude", "claude --append-system-prompt x -c")]


def rules(cmd: str, *, bg: bool = False) -> list:
    return sorted({f.rule for f in pg.check_bash(cmd, background=bg,
                                                 ancestors=ANCESTORS)})


class MeasuredOnBs2(unittest.TestCase):
    """bash -c execs its last simple command in place; anything after
    the kill keeps the shell — and its command line — alive."""

    def test_single_remote_command_is_exec_d(self):
        self.assertEqual(rules("ssh bs2 'pkill -f zqnomatch_77'"), [])

    def test_kill_as_the_tail_of_a_list_is_exec_d(self):
        self.assertEqual(rules("ssh bs2 'cd /tmp; pkill -f zqnomatch_78'"), [])

    def test_anything_after_the_kill_keeps_the_carrier(self):
        self.assertEqual(rules("ssh bs2 'pkill -f zqnomatch_79; true'"), ["self-kill"])

    def test_bracket_without_a_bare_copy_is_safe(self):
        self.assertEqual(rules("ssh bs2 'pkill -f \"[z]qnomatch_80\"; true'"), [])

    def test_bracket_with_a_bare_copy_dies(self):
        self.assertEqual(rules("ssh bs2 'pkill -f \"[z]qnomatch_81\"; echo zqnomatch_81'"),
                         ["self-kill"])

    def test_script_on_stdin_is_in_no_argv(self):
        self.assertEqual(rules("ssh bs2 'bash -s' <<'EOF'\npkill -f zqnomatch_82\n"
                               "echo still-alive zqnomatch_82\nEOF"), [])


class SelfKill(unittest.TestCase):

    def test_local_pkill_always_has_its_carrier(self):
        # the Bash tool's wrapper runs `&& pwd -P` after the command
        self.assertEqual(rules("pkill -f run-baseline.sh"), ["self-kill"])

    def test_bracket_defeated_by_a_later_step(self):
        self.assertEqual(rules("pkill -f '[r]un-baseline.sh'; nohup ./run-baseline.sh &"),
                         ["self-kill"])

    def test_bracket_alone_is_fine(self):
        self.assertEqual(rules("pkill -f '[r]un-baseline.sh'"), [])

    def test_pgrep_into_kill(self):
        self.assertEqual(rules('kill $(pgrep -f "lm_eval.*ifeval") 2>/dev/null; echo done'),
                         ["self-kill"])
        self.assertEqual(rules('pgrep -f "ep-p3.py" | xargs -r kill'), ["self-kill"])

    def test_captured_pid_list_later_killed(self):
        self.assertEqual(rules("ssh bs2 'P=$(pgrep -f \"curl -s http://127.0.0.1:8237\"); kill $P'"),
                         ["self-kill"])

    def test_for_loop_kill(self):
        self.assertEqual(rules("for p in $(pgrep -f omk_eval.py); do kill -KILL $p; done; echo omk_eval.py"),
                         ["self-kill"])

    def test_unanchored_alternative(self):
        self.assertEqual(rules("ssh bs2 'pkill -f \"^bash a|^bash b|steadyload2\"; sleep 1; echo x'"),
                         ["self-kill"])

    def test_ps_grep_kill_with_a_bare_copy(self):
        self.assertEqual(rules("ps -eo pid,args | grep '[p]erplexity' | awk '{print $1}' | xargs kill; echo perplexity"),
                         ["self-kill"])

    def test_backgrounded_subshell_forks_the_carrier(self):
        # `( ... ) &` keeps this shell's argv, so it kills itself
        self.assertEqual(rules("( sleep 5; kill $(pgrep -f 'llama-server.*109e') ) &\necho started"),
                         ["self-kill"])

    def test_the_session_itself(self):
        self.assertEqual(rules("pkill -f claude"), ["self-kill"])

    def test_sshd_and_everything(self):
        self.assertEqual(rules("ssh bs2 'systemctl stop sshd'"), ["kills-ssh"])
        self.assertEqual(rules("ssh bs2 'kill -9 -1'"), ["kills-ssh"])
        self.assertEqual(rules("ssh bs2 'systemctl restart sshd'"), [])


class NotAFault(unittest.TestCase):
    """False positives the corpus showed — each one must stay clean."""

    def test_listing_is_not_a_fault(self):
        self.assertEqual(rules("pgrep -af python"), [])
        self.assertEqual(rules("ps aux | grep python"), [])
        self.assertEqual(rules("for p in $(pgrep -f x); do echo $p; done"), [])

    def test_dollar_dollar_exclusion(self):
        self.assertEqual(rules('SELF=$$; for p in $(pgrep -f "c-4.99.17.vsix"); do '
                               '[ "$p" != "$SELF" ] && kill "$p"; done'), [])

    def test_filters_after_pgrep_a(self):
        self.assertEqual(rules("pgrep -af sp-mtp-family | grep -v grep | awk '{print $1}' "
                               "| while read p; do kill \"$p\"; done"), [])

    def test_bre_alternation_in_plain_grep(self):
        self.assertEqual(rules("pgrep -af s3d-gate.sh | grep -v 'grep\\|pgrep' | "
                               "awk '{print $1}' | xargs -r kill"), [])

    def test_awk_conjunction(self):
        self.assertEqual(rules("P=$(ps -eo pid,args | awk '/timeout -k 60/ && /index\\.ts/ "
                               "&& !/awk/ {print $1}')\n[ -n \"$P\" ] && kill $P"), [])

    def test_head_one_keeps_the_oldest(self):
        self.assertEqual(rules("OLD=$(ps -eo pid,args | awk '/[a]b-auto\\.sh/ {print $1}' | head -1)\n"
                               "kill \"$OLD\"; nohup ./ab-auto.sh &"), [])

    def test_detached_script_does_not_carry_the_wrapper(self):
        self.assertEqual(rules("cat > /tmp/q.sh <<'B'\npkill -f 'srv.*8099'\nB\n"
                               "nohup bash /tmp/q.sh > /tmp/q.log 2>&1 &\necho queued"), [])

    def test_anchor_is_honoured(self):
        self.assertEqual(rules('pkill -f "llama-server.*--no-warmup$" 2>/dev/null; sleep 2'), [])

    def test_unresolvable_pattern_gets_no_opinion(self):
        self.assertEqual(rules('pkill -f "$PATTERN"; echo done'), [])

    def test_unparseable_gets_no_opinion(self):
        self.assertEqual(rules("pkill -f 'unterminated"), [])

    def test_override_marker(self):
        self.assertEqual(rules("pkill -f x; echo x  # process-guard: allow"), [])


class SelfMatchingWaiters(unittest.TestCase):

    def test_local_waiter_never_ends(self):
        self.assertEqual(rules('until ! pgrep -f "build-pipeline.ts cuda"; do sleep 30; done; '
                               'echo finished', bg=True), ["self-match"])

    def test_remote_waiter_never_ends(self):
        self.assertEqual(rules("ssh bs2 'while pgrep -f \"cuda.sh --clean\"; do sleep 20; done; "
                               "echo ok'", bg=True), ["self-match"])

    def test_bracketed_remote_waiter_is_fine(self):
        self.assertEqual(rules("ssh bs2 'while pgrep -f \"[c]uda.sh --clean\"; do sleep 20; "
                               "done; echo ok'", bg=True), [])

    def test_single_remote_pgrep_is_exec_d(self):
        self.assertEqual(rules("until ! ssh bs2 'pgrep -f fse2-gate.sh'; do sleep 60; done",
                               bg=True), [])

    def test_phantom_running(self):
        self.assertEqual(rules("ssh bs2 'tail -3 log; pgrep -f gate-3452 >/dev/null && "
                               "echo RUNNING || echo done'"), ["self-match"])


class BlindWaiters(unittest.TestCase):

    def test_success_only(self):
        self.assertEqual(rules("until [ -e /w/DONE ]; do sleep 30; done; cat /w/r", bg=True),
                         ["blind-waiter"])
        self.assertEqual(rules("ssh bs2 'until grep -q EDGE-DONE /x/gate.out; do sleep 25; done'",
                               bg=True), ["blind-waiter"])
        self.assertEqual(rules("until ssh bs2 'test -e /x/DONE'; do sleep 15; done", bg=True),
                         ["blind-waiter"])

    def test_covered_ways(self):
        for cmd in (
            "until [ -e /w/DONE ] || ! kill -0 $PID 2>/dev/null; do sleep 30; done",
            "ssh bs2 'until grep -qE \"EDGE-DONE|FAILED\" /x/gate.out; do sleep 25; done'",
            "until grep -qE 'E4B-DONE|VOID: GPU1|port bound' /x.out; do sleep 25; done",
            "until grep -q '^EXIT=' b.log; do sleep 20; done",
            "i=0; until [ -e DONE ]; do i=$((i+1)); [ $i -ge 60 ] && break; sleep 10; done",
            "timeout 3h bash -c 'until [ -e DONE ]; do sleep 30; done'",
            "while kill -0 3952274 2>/dev/null; do sleep 600; done; tail -3 log",
            "for i in $(seq 60); do [ -e DONE ] && break; sleep 10; done",
            "while ssh -o ConnectTimeout=5 -p 12554 root@h \"pgrep -f '[l]lama-quantize'\"; do sleep 30; done",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(rules(cmd, bg=True), [])

    def test_foreground_loops_are_bounded_by_the_tool(self):
        self.assertEqual(rules("until [ -e DONE ]; do sleep 1; done"), [])

    def test_monitor_filter_must_see_failure(self):
        self.assertEqual([f.rule for f in pg.check_monitor(
            "tail -f run.log | grep --line-buffered 'elapsed_steps='")], ["blind-waiter"])
        self.assertEqual(pg.check_monitor(
            "tail -f run.log | grep -E --line-buffered 'elapsed_steps=|Traceback|Error'"), [])

    def test_monitor_loop_that_emits_failures(self):
        self.assertEqual(pg.check_monitor(
            'while true; do hits=$(grep -aE "FAIL|Traceback|DONE" /x.log); '
            '[ -n "$hits" ] && echo "$hits"; echo "$hits" | grep -q DONE && break; '
            'sleep 30; done'), [])


class Prompts(unittest.TestCase):

    def test_status_check_gets_the_failure_note(self):
        out = pg.augment_prompt("check whether the quant build finished")
        self.assertIn(pg.PROMPT_NOTE, out)

    def test_idempotent_and_selective(self):
        once = pg.augment_prompt("check the build status")
        self.assertIsNone(pg.augment_prompt(once))
        self.assertIsNone(pg.augment_prompt("check whether it finished or failed"))
        self.assertIsNone(pg.augment_prompt("/loop check the build"))
        self.assertIsNone(pg.augment_prompt("<<autonomous-loop-dynamic>>"))
        self.assertIsNone(pg.augment_prompt("remind me to call mum"))


class Message(unittest.TestCase):

    def test_self_match_explains_the_negated_crash_check(self):
        """opencoti's Monitor: `until [ -f out ] || ! pgrep -f "gate.sh kld …"`
        over ssh. The negated test is the crash check, and the message
        must say that is what never fires, not only that a `while` loops."""
        cmd = ("ssh bs2 'until [ -s kld-0909.out ] || ! pgrep -f \"gate.sh kld "
               "2610080909001\" >/dev/null; do sleep 20; done; tail -5 kld-0909.out'")
        findings = pg.check_monitor(cmd, ancestors=ANCESTORS)
        self.assertEqual(sorted({f.rule for f in findings}), ["self-match"])
        text = pg.render(findings)
        self.assertIn("`! pgrep` always false", text)
        self.assertIn("crash check) never fires", text)
        self.assertIn("[x]yz", text)

    def test_says_what_and_how_and_never_asks_the_user(self):
        text = pg.render(pg.check_bash("pkill -f run-baseline.sh; echo x",
                                       ancestors=ANCESTORS))
        self.assertIn("run-baseline.sh", text)
        self.assertIn("Fix:", text)
        self.assertIn("does not need the user", text)
        self.assertNotIn("wait for the user", text.lower())

    def test_bash_tool_cmdline_escapes_like_the_tool(self):
        self.assertIn("eval 'echo '\"'\"'x'\"'\"'' && pwd -P",
                      pg.bash_tool_cmdline("echo 'x'"))


class HookIntegration(unittest.TestCase):

    def _event(self, tool, tool_input, **kw):
        return {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": "/w",
                "tool_name": tool, "tool_input": tool_input, **kw}

    def test_deny_with_reason(self):
        from claude_hooks.hooks.pre_tool_use import handle
        out = handle(event=self._event("Bash", {"command": "pkill -f abc; echo abc"}),
                     config={}, providers=[])
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("self-kill", hso["permissionDecisionReason"])

    def test_clean_command_passes_through(self):
        from claude_hooks.hooks.pre_tool_use import handle
        self.assertIsNone(handle(event=self._event("Bash", {"command": "ls -la"}),
                                 config={}, providers=[]))

    def test_background_flag_selects_the_waiter_rule(self):
        from claude_hooks.hooks.pre_tool_use import process_guard_response as r
        cmd = "until [ -e /w/DONE ]; do sleep 30; done"
        self.assertIsNone(r(self._event("Bash", {"command": cmd}), {}))
        self.assertEqual(r(self._event("Bash", {"command": cmd, "run_in_background": True}),
                           {})["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_switches(self):
        from claude_hooks.hooks.pre_tool_use import process_guard_response as r
        off = {"hooks": {"pre_tool_use": {"process_guard": {"enabled": False}}}}
        self.assertIsNone(r(self._event("Bash", {"command": "pkill -f a; echo a"}), off))
        no_wait = {"hooks": {"pre_tool_use": {"process_guard": {"waiters": False}}}}
        self.assertIsNone(r(self._event("Bash", {"command": "until [ -e D ]; do sleep 3; done",
                                                 "run_in_background": True}), no_wait))

    def test_prompt_rewrite_has_no_decision(self):
        from claude_hooks.hooks.pre_tool_use import process_guard_response as r
        out = r(self._event("ScheduleWakeup", {"prompt": "check if the run finished",
                                               "delaySeconds": 1200}), {})
        hso = out["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertEqual(hso["updatedInput"]["delaySeconds"], 1200)
        self.assertIn(pg.PROMPT_NOTE, hso["updatedInput"]["prompt"])

    def test_guard_failure_never_blocks(self):
        from claude_hooks.hooks.pre_tool_use import process_guard_response as r
        with mock.patch.object(pg, "check_bash", side_effect=RuntimeError("boom")):
            self.assertIsNone(r(self._event("Bash", {"command": "pkill -f a; echo a"}), {}))

    def test_marker_part_guards(self):
        from claude_hooks import hook_parts
        ev = self._event("Bash", {"command": "pkill -f abc; echo abc"})
        self.assertIn("guards", hook_parts.parse_marker("keep: memory, guards"))
        self.assertIsNone(hook_parts.run("PreToolUse", event=ev, config={}, providers=[],
                                         keep=frozenset({"memory"})))
        out = hook_parts.run("PreToolUse", event=ev, config={}, providers=[],
                             keep=frozenset({"guards"}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class RunInjectsAncestors(unittest.TestCase):

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux /proc")
    def test_run_adds_process_ancestors_for_bash(self):
        seen = []
        env = {"CLAUDE_HOOKS_DAEMON_DISABLE": "1"}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(sys, "argv", ["run.py", "PreToolUse"]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                    {"session_id": "s", "tool_name": "Bash",
                     "tool_input": {"command": "ls"}}))), \
                mock.patch("claude_hooks.dispatcher.dispatch",
                           side_effect=lambda name, ev: seen.append(ev) or 0):
            import importlib
            import run as run_mod
            importlib.reload(run_mod)
            run_mod.main()
        anc = seen[0]["process_ancestors"]
        self.assertIsInstance(anc, list)
        self.assertTrue(all(len(a) == 2 for a in anc))

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux /proc")
    def test_ancestors_skip_the_hook_launchers(self):
        for comm, cmdline in pg.ancestors_from_proc():
            self.assertNotIn("claude-hook", cmdline)


if __name__ == "__main__":
    unittest.main()
