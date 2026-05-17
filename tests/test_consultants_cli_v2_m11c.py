"""M11c CLI tests — ``skill-eval tool_executor`` sub-subparser
wiring in ``consultants.cli``.

Three concerns:

1. **Argv parsing** — every flag the sub-subparser exposes lands
   on the ``Namespace`` with the expected type / default, and the
   ``fn`` dispatch points at ``cmd_skill_eval_tool_executor``.
2. **Mode requirement** — exactly one of ``--dry-run`` /
   ``--live`` must be set; missing both exits 2.
3. **Handler argv translation** — the Namespace gets translated to
   the bench script's argv shape correctly. The bench's ``main``
   is monkeypatched so no actual run happens; we just assert on
   the argv it received.

These tests run on the **main** env (no cloud-side deps); they
only exercise the client-side cli.py wiring.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


from consultants.cli import (
    build_parser,
    cmd_skill_eval_tool_executor,
)


# ============================================================== #
# argv → fn dispatch
# ============================================================== #

class TestSkillEvalToolExecutorArgv(unittest.TestCase):

    def test_minimal_dry_run_dispatches_to_handler(self) -> None:
        args = build_parser().parse_args([
            "skill-eval", "tool_executor", "--dry-run",
        ])
        self.assertEqual(args.fn.__name__, "cmd_skill_eval_tool_executor")
        self.assertTrue(args.dry_run)
        self.assertFalse(args.live)
        # Defaults that the handler reads.
        self.assertFalse(args.accept_cost)
        self.assertIsNone(args.models)
        self.assertIsNone(args.ollama_base)
        self.assertIsNone(args.judge_model)
        self.assertIsNone(args.trials)
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.tier)
        self.assertIsNone(args.id)
        self.assertFalse(args.smoke)

    def test_full_flag_set_parses(self) -> None:
        args = build_parser().parse_args([
            "skill-eval", "tool_executor",
            "--live", "--accept-cost",
            "--models", "kimi-k2.6:cloud,gemma4:31b-cloud",
            "--ollama-base", "http://192.168.178.2:11433",
            "--judge-model", "gemma4:31b-cloud",
            "--trials", "2",
            "--output-dir", "/tmp/te-out",
            "--tier", "trivial",
            "--tier", "easy",
            "--id", "trivial-01-find-symbol",
            "--id", "easy-01-grep-read-chain",
            "--smoke",
        ])
        self.assertTrue(args.live)
        self.assertTrue(args.accept_cost)
        self.assertEqual(args.models,
                         "kimi-k2.6:cloud,gemma4:31b-cloud")
        self.assertEqual(args.ollama_base,
                         "http://192.168.178.2:11433")
        self.assertEqual(args.judge_model, "gemma4:31b-cloud")
        self.assertEqual(args.trials, 2)
        self.assertEqual(args.output_dir, "/tmp/te-out")
        self.assertEqual(args.tier, ["trivial", "easy"])
        self.assertEqual(args.id, [
            "trivial-01-find-symbol",
            "easy-01-grep-read-chain",
        ])
        self.assertTrue(args.smoke)

    def test_dry_run_and_live_are_mutually_exclusive(self) -> None:
        """Both modes set at once must fail at parse time."""
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "skill-eval", "tool_executor",
                "--dry-run", "--live",
            ])

    def test_mode_required(self) -> None:
        """Missing both --dry-run and --live must fail."""
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "skill-eval", "tool_executor",
            ])

    def test_judge_model_empty_string_is_allowed(self) -> None:
        """``--judge-model ''`` is the documented way to disable
        the judge. The parser must accept it without coercing to
        None (the handler distinguishes empty from unset)."""
        args = build_parser().parse_args([
            "skill-eval", "tool_executor", "--dry-run",
            "--judge-model", "",
        ])
        self.assertEqual(args.judge_model, "")

    def test_tier_choices_validated(self) -> None:
        """An unknown tier must be rejected by argparse."""
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "skill-eval", "tool_executor", "--dry-run",
                "--tier", "impossible",
            ])

    def test_trials_must_be_int(self) -> None:
        """argparse coerces ``--trials`` via ``type=int``; a
        non-numeric value must error out."""
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "skill-eval", "tool_executor", "--dry-run",
                "--trials", "many",
            ])


# ============================================================== #
# Handler argv translation
# ============================================================== #

class TestSkillEvalToolExecutorHandlerForwarding(unittest.TestCase):
    """The handler is a thin Namespace → argv translator. These
    tests monkeypatch the bench's ``main`` and assert on the
    argv it receives. No bench code runs."""

    def _call(self, args_ns) -> list:
        """Invoke the handler with the bench's ``main`` patched
        out; return the argv list it would have received."""
        seen: dict = {}

        def _fake_main(argv: list[str]) -> int:
            seen["argv"] = list(argv)
            return 0

        with mock.patch(
            "benchmarks.consultants.tool_executor_bench.main",
            _fake_main,
        ):
            rc = cmd_skill_eval_tool_executor(args_ns, base="")
        self.assertEqual(rc, 0)
        return seen.get("argv", [])

    def _minimal_ns(self, **overrides) -> SimpleNamespace:
        base = dict(
            dry_run=True, live=False, accept_cost=False,
            models=None, ollama_base=None, judge_model=None,
            trials=None, output_dir=None,
            tier=None, id=None, smoke=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_dry_run_minimal(self) -> None:
        argv = self._call(self._minimal_ns())
        self.assertEqual(argv, ["--dry-run"])

    def test_live_with_accept_cost(self) -> None:
        argv = self._call(self._minimal_ns(
            dry_run=False, live=True, accept_cost=True,
        ))
        self.assertEqual(argv, ["--live", "--accept-cost"])

    def test_models_flag_forwarded(self) -> None:
        argv = self._call(self._minimal_ns(
            models="kimi-k2.6:cloud,gemma4:31b-cloud",
        ))
        # Find adjacent --models <value> pair.
        i = argv.index("--models")
        self.assertEqual(argv[i + 1],
                         "kimi-k2.6:cloud,gemma4:31b-cloud")

    def test_ollama_base_forwarded(self) -> None:
        argv = self._call(self._minimal_ns(
            ollama_base="http://192.168.178.2:11433",
        ))
        i = argv.index("--ollama-base")
        self.assertEqual(argv[i + 1],
                         "http://192.168.178.2:11433")

    def test_judge_model_empty_forwarded_explicitly(self) -> None:
        """An empty ``--judge-model`` is the documented way to
        disable the judge. The handler must forward the empty
        string, NOT swallow it as "unset"."""
        argv = self._call(self._minimal_ns(judge_model=""))
        self.assertIn("--judge-model", argv)
        i = argv.index("--judge-model")
        self.assertEqual(argv[i + 1], "")

    def test_judge_model_none_is_omitted(self) -> None:
        """When the user didn't pass ``--judge-model`` at all
        (Namespace value is None), the handler must NOT forward
        the flag — the bench picks its own default."""
        argv = self._call(self._minimal_ns(judge_model=None))
        self.assertNotIn("--judge-model", argv)

    def test_trials_forwarded_as_string(self) -> None:
        argv = self._call(self._minimal_ns(trials=3))
        i = argv.index("--trials")
        self.assertEqual(argv[i + 1], "3")

    def test_trials_none_is_omitted(self) -> None:
        """``--trials`` defaults to None ⇒ omit from argv so the
        bench's own DEFAULT_TRIALS applies."""
        argv = self._call(self._minimal_ns(trials=None))
        self.assertNotIn("--trials", argv)

    def test_tier_repeated_correctly(self) -> None:
        argv = self._call(self._minimal_ns(
            tier=["trivial", "easy"],
        ))
        # --tier appears twice with the right values.
        idxs = [i for i, v in enumerate(argv) if v == "--tier"]
        self.assertEqual(len(idxs), 2)
        values = [argv[i + 1] for i in idxs]
        self.assertEqual(values, ["trivial", "easy"])

    def test_id_repeated_correctly(self) -> None:
        argv = self._call(self._minimal_ns(
            id=["trivial-01-find-symbol", "easy-01-grep-read-chain"],
        ))
        idxs = [i for i, v in enumerate(argv) if v == "--id"]
        self.assertEqual(len(idxs), 2)
        values = [argv[i + 1] for i in idxs]
        self.assertEqual(values, [
            "trivial-01-find-symbol", "easy-01-grep-read-chain",
        ])

    def test_output_dir_forwarded(self) -> None:
        argv = self._call(self._minimal_ns(output_dir="/tmp/abc"))
        i = argv.index("--output-dir")
        self.assertEqual(argv[i + 1], "/tmp/abc")

    def test_smoke_forwarded(self) -> None:
        argv = self._call(self._minimal_ns(smoke=True))
        self.assertIn("--smoke", argv)

    def test_full_flag_set_full_argv(self) -> None:
        """End-to-end: the maximally-loaded Namespace produces a
        fully-formed argv. Pin the exact list shape so accidental
        drift surfaces as a test failure."""
        argv = self._call(self._minimal_ns(
            dry_run=False, live=True, accept_cost=True,
            models="m1,m2", ollama_base="http://x",
            judge_model="judge:cloud", trials=2,
            output_dir="/tmp/o",
            tier=["trivial"], id=["q1"],
            smoke=True,
        ))
        self.assertEqual(argv, [
            "--live", "--accept-cost",
            "--models", "m1,m2",
            "--ollama-base", "http://x",
            "--judge-model", "judge:cloud",
            "--trials", "2",
            "--output-dir", "/tmp/o",
            "--tier", "trivial",
            "--id", "q1",
            "--smoke",
        ])


# ============================================================== #
# End-to-end: parser → handler → bench (dry-run mode)
# ============================================================== #

class TestSkillEvalToolExecutorEndToEnd(unittest.TestCase):
    """Confirm the parser + handler compose correctly. We patch
    the bench's main to capture argv but otherwise let the full
    dispatch path run."""

    def test_dispatch_via_main(self) -> None:
        """``consultants.cli.main([...])`` reaches the bench."""
        from consultants import cli as _cli

        seen: dict = {}

        def _fake_main(argv: list[str]) -> int:
            seen["argv"] = list(argv)
            return 0

        with mock.patch(
            "benchmarks.consultants.tool_executor_bench.main",
            _fake_main,
        ):
            rc = _cli.main([
                "skill-eval", "tool_executor", "--dry-run",
                "--smoke", "--trials", "1",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("--dry-run", seen["argv"])
        self.assertIn("--smoke", seen["argv"])
        self.assertIn("--trials", seen["argv"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
