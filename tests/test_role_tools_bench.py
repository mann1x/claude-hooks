"""M-B bench harness.

The bench exists to answer one question — does ``[tools] all_roles``
buy enough to pay for itself — and the answer will be used to set a
default. That makes the *harness* a load-bearing artifact: a bench that
scores generously produces a number nobody should act on, and
``tool_executor`` is the standing proof that a suite can pass and the
feature still be wrong.

So the tests here are mostly about the oracle being honest:

* the planted ground truth is checked **against the fixture**, not
  taken on faith from the question file (a "planted falsehood" that is
  quietly true of the code makes the whole corpus meaningless);
* a verdict that merely repeats a claim is not scored as catching it;
* the dry run cannot produce a passing-looking result.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmarks.consultants.role_tools_bench import (  # noqa: E402
    SUITE_DIR,
    DetectTrial,
    _StubChat,
    flagged,
    load_detect_questions,
    render_report,
    run_detect_trial,
    suite_hash,
    summarize,
)


def _fixture_body(q) -> str:
    d = SUITE_DIR / "fixtures" / q.fixtures_subdir
    return "".join(p.read_text(encoding="utf-8") for p in sorted(d.glob("*.py")))


# ===================================================================== #
# The corpus, and whether its ground truth is real
# ===================================================================== #
class TestCorpus(unittest.TestCase):
    def setUp(self):
        self.qs = load_detect_questions()

    def test_all_six_questions_parse(self):
        self.assertEqual(len(self.qs), 6)
        for q in self.qs:
            self.assertTrue(q.research, f"{q.id}: empty RESEARCH")
            self.assertTrue(q.task, f"{q.id}: empty task")

    def test_manifest_matches_the_files_on_disk(self):
        """A question dropped from the manifest still runs but stops
        being hashed, so drift in it would go unnoticed."""
        from benchmarks.consultants.harness import load_suite_manifest
        man = load_suite_manifest(SUITE_DIR)
        self.assertEqual(sorted(man.manifest), sorted(q.id for q in self.qs))

    def test_suite_hash_is_stable_and_nonempty(self):
        h = suite_hash()
        self.assertEqual(len(h), 64)
        self.assertEqual(h, suite_hash())

    def test_planted_false_is_actually_absent_from_the_fixture(self):
        """The load-bearing check. If a token we call fabricated is in
        fact present in the code, then a tooled critic that verifies it
        and stays silent is *correct* and we score it as a miss — the
        bench would then punish exactly the behaviour it is meant to
        reward."""
        for q in self.qs:
            body = _fixture_body(q)
            for tok in q.planted_false:
                self.assertNotIn(tok, body,
                                 f"{q.id}: '{tok}' is planted as false but "
                                 f"exists in the fixture")

    def test_planted_true_is_actually_present_in_the_fixture(self):
        """The mirror. A 'true' claim that the fixture does not support
        makes every flag on it a false positive by our count, which
        would drag precision down for the right answer."""
        for q in self.qs:
            body = _fixture_body(q)
            for tok in q.planted_true:
                self.assertIn(tok, body,
                              f"{q.id}: '{tok}' is planted as true but is "
                              f"absent from the fixture")

    def test_every_cite_in_the_research_resolves(self):
        """The check that hand-auditing failed to do.

        v1.0 shipped with four `path:line` cites that pointed at the
        wrong line — including one in the *control*, where the only
        correct number of flags is zero. The tooled critic looked them
        up, correctly reported the drift, and the harness scored it as
        a false positive: the bench punished the model for being right
        and understated precision as 69.2% when it was 100%.

        Substring checks on the planted tokens cannot see this, because
        the symbol really is in the file — just not on the cited line.
        ``expected_drift`` below is the allow-list of cites that are
        wrong on purpose; everything else must resolve to a line that
        plausibly supports the claim.
        """
        #: (question id, cite) pairs that are deliberately false.
        expected_drift = {
            ("easy-01-fabricated-file", "retry_state.py:12"),
            ("hard-02-line-drift", "retry.py:1"),
        }
        cite_re = re.compile(r"\b([\w.]+\.py):(\d+)")
        checked = 0
        for q in self.qs:
            d = SUITE_DIR / "fixtures" / q.fixtures_subdir
            for m in cite_re.finditer(q.research):
                fname, lineno = m.group(1), int(m.group(2))
                cite = f"{fname}:{lineno}"
                if (q.id, cite) in expected_drift:
                    continue
                checked += 1
                path = d / fname
                self.assertTrue(path.is_file(),
                                f"{q.id}: cites missing file {fname}")
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertTrue(
                    0 < lineno <= len(lines),
                    f"{q.id}: {cite} is out of range ({len(lines)} lines)")
                body = lines[lineno - 1].strip()
                self.assertTrue(
                    body, f"{q.id}: {cite} points at a blank line")
        self.assertGreater(checked, 10, "cite regex matched almost nothing")

    def test_control_question_has_no_drifting_cites(self):
        """Stated separately because it is the one that matters most.
        Any wrong cite in the control makes a correct flag score as a
        false positive, which is the only thing the control measures."""
        q = next(x for x in self.qs if x.is_control)
        d = SUITE_DIR / "fixtures" / q.fixtures_subdir
        for m in re.finditer(r"\b([\w.]+\.py):(\d+)", q.research):
            lines = (d / m.group(1)).read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[int(m.group(2)) - 1].strip(),
                            f"control cites blank line {m.group(0)}")

    def test_line_drift_question_really_does_drift(self):
        """hard-02 plants a real symbol at a wrong line, so the
        substring check above cannot see the falsehood. Verify the
        claim the way a tooled critic would have to."""
        q = next(x for x in self.qs if x.id == "hard-02-line-drift")
        lines = (SUITE_DIR / "fixtures" / q.fixtures_subdir
                 / "retry.py").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("should_retry", lines[0],
                         "the cited line 1 must NOT define should_retry")
        self.assertTrue(any("def should_retry" in ln for ln in lines),
                        "but the symbol must exist somewhere, or this is "
                        "just another nonexistent-symbol question")

    def test_no_true_token_shares_a_sentence_with_a_false_one(self):
        """v1.1's precision was understated at 78.6% by this defect.

        The oracle scores a flag by finding a doubt word near a token.
        When a "true" token sits in the same sentence as the fabricated
        claim, a *correct* verdict has to name both — "there is no
        `reset_breaker`; the class only implements `record_failure` and
        `is_open`" doubts one and confirms the other, and the window
        cannot tell them apart. The token then counts as a false alarm
        for a critic that did exactly the right thing.

        A token that cannot be flagged independently of the falsehood
        is not a usable control, so it must not be listed as one.
        """
        for q in self.qs:
            if not q.planted_false:
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", q.research):
                hits_false = [f for f in q.planted_false if f in sentence]
                if not hits_false:
                    continue
                for tok in q.planted_true:
                    self.assertNotIn(
                        tok, sentence,
                        f"{q.id}: '{tok}' is a precision control but shares "
                        f"a sentence with the planted falsehood "
                        f"{hits_false[0]!r} — a correct catch would score "
                        f"as a false positive")

    def test_exactly_one_control_question(self):
        """Precision needs a question where the only correct number of
        flags is zero. More than one and the control dominates; none and
        precision is only measured against questions that also contain a
        real falsehood."""
        controls = [q for q in self.qs if q.is_control]
        self.assertEqual([q.id for q in controls], ["medium-02-all-true"])

    def test_the_suite_carries_enough_true_claims_to_measure_precision(self):
        """A corpus of pure falsehood would let a flag-everything critic
        score perfectly, so precision needs true claims to be wrong
        about — but they have to be *scorable* ones.

        This is asserted across the suite rather than per question. v1.1
        listed a true token in every question and two of them were
        unusable: a correct verdict could not avoid naming them while
        doubting the falsehood beside them, so they measured the
        oracle's proximity window rather than the critic's judgement.
        Dropping an unscorable control is an improvement; the bar is
        that enough remain, concentrated in the control question where
        the correct number of flags is unambiguously zero.
        """
        total = sum(len(q.planted_true) for q in self.qs)
        self.assertGreaterEqual(total, 4, "too few precision controls")
        control = next(q for q in self.qs if q.is_control)
        self.assertGreaterEqual(
            len(control.planted_true), 3,
            "the control carries the uncontaminated precision signal")

    def test_fixture_directories_exist(self):
        for q in self.qs:
            d = SUITE_DIR / "fixtures" / q.fixtures_subdir
            self.assertTrue(d.is_dir(), f"{q.id}: missing {d}")
            self.assertTrue(list(d.glob("*.py")), f"{q.id}: no fixture code")


# ===================================================================== #
# The oracle
# ===================================================================== #
class TestFlagged(unittest.TestCase):
    def test_doubt_near_the_token_counts(self):
        self.assertTrue(flagged(
            "The research cites retry_state.py, but no such file exists.",
            "retry_state.py"))

    def test_bare_repetition_does_not_count(self):
        """A critic that restates a fabricated claim approvingly has
        caught nothing. Scoring on presence alone would credit it."""
        self.assertFalse(flagged(
            "As the research notes, retry_state.py holds the state.",
            "retry_state.py"))

    def test_doubt_too_far_away_does_not_count(self):
        far = ("retry_state.py is mentioned. " + "filler. " * 80
               + "Something else here does not exist.")
        self.assertFalse(flagged(far, "retry_state.py"))

    def test_absent_token_is_not_flagged(self):
        self.assertFalse(flagged("Everything checks out.", "retry_state.py"))

    def test_second_occurrence_can_carry_the_doubt(self):
        """Models often introduce a claim neutrally and challenge it
        later; only scanning the first hit would miss that."""
        txt = ("retry_state.py is cited in claim 3. " + "filler. " * 60
               + "On checking, retry_state.py does not exist.")
        self.assertTrue(flagged(txt, "retry_state.py"))

    def test_is_case_insensitive(self):
        self.assertTrue(flagged("MAX_ATTEMPTS is WRONG here", "max_attempts"))

    def test_empty_inputs_are_safe(self):
        self.assertFalse(flagged("", "x"))
        self.assertFalse(flagged("something", ""))


# ===================================================================== #
# Aggregation
# ===================================================================== #
def _trial(arm="tooled", caught=(), missed=(), fps=(), **kw):
    return DetectTrial(question_id=kw.pop("qid", "q"), tier="easy", arm=arm,
                       trial_idx=0, caught=list(caught), missed=list(missed),
                       false_positives=list(fps), **kw)


class TestSummarize(unittest.TestCase):
    def test_recall_and_precision(self):
        s = summarize([
            _trial(caught=["a"], missed=["b"]),
            _trial(caught=["c"], fps=["t1"]),
        ])["tooled"]
        self.assertEqual(s["planted_false"], 3)
        self.assertAlmostEqual(s["recall"], 2 / 3)
        # 2 real flags out of 3 total flags
        self.assertAlmostEqual(s["precision"], 2 / 3)

    def test_control_false_positives_hit_precision(self):
        """The control has no planted falsehood, so it cannot move
        recall — its whole job is to move precision. If it did not,
        a flag-everything critic would look flawless."""
        s = summarize([
            _trial(caught=["a"]),
            _trial(qid="ctrl", fps=["t1", "t2"]),
        ])["tooled"]
        self.assertEqual(s["recall"], 1.0)
        self.assertAlmostEqual(s["precision"], 1 / 3)

    def test_errored_trials_are_excluded_but_counted(self):
        """An erroring trial must not be silently scored as a miss —
        that would let an infrastructure failure read as a model
        result."""
        s = summarize([_trial(caught=["a"]), _trial(missed=["b"], error="boom")])
        self.assertEqual(s["tooled"]["errors"], 1)
        self.assertEqual(s["tooled"]["recall"], 1.0)

    def test_no_data_yields_none_not_zero(self):
        """0% recall and 'never ran' are different claims."""
        s = summarize([])
        self.assertIsNone(s["tooled"]["recall"])
        self.assertIsNone(s["tooled"]["precision"])

    def test_both_arms_always_present(self):
        s = summarize([_trial(arm="tooled", caught=["a"])])
        self.assertIn("untooled", s)
        self.assertEqual(s["untooled"]["trials"], 0)

    def test_trial_recall_property(self):
        self.assertAlmostEqual(_trial(caught=["a"], missed=["b", "c"]).recall,
                               1 / 3)
        self.assertIsNone(_trial().recall)

    def test_to_dict_is_json_serialisable(self):
        json.dumps(_trial(caught=["a"]).to_dict())


class TestRender(unittest.TestCase):
    def test_report_states_the_lower_bound_caveat(self):
        """The keyword oracle undercounts, and a report that omits that
        invites someone to read a marginal recall number as final."""
        out = render_report(summarize([_trial(caught=["a"])]),
                            [_trial(caught=["a"])])
        self.assertIn("lower bound", out)

    def test_report_renders_with_no_trials(self):
        self.assertIn("n/a", render_report(summarize([]), []))


# ===================================================================== #
# The trial runner
# ===================================================================== #
class TestRunDetectTrial(unittest.TestCase):
    def setUp(self):
        self.qs = {q.id: q for q in load_detect_questions()}

    def test_untooled_arm_advertises_no_tools(self):
        chat = _StubChat()
        t = run_detect_trial(self.qs["easy-01-fabricated-file"],
                             arm="untooled", trial_idx=0,
                             chat_client=chat, model="stub")
        self.assertIsNone(t.error)
        self.assertEqual(t.tool_calls, 0)
        self.assertTrue(all("tools" not in p for p in
                            getattr(chat, "payloads", [])))

    def test_tooled_arm_actually_executes_against_the_fixture(self):
        """Not just 'tools were offered' — the executor has to reach
        the fixture directory. A cwd bug here would silently produce an
        arm that looks tooled and verifies nothing."""
        t = run_detect_trial(self.qs["easy-01-fabricated-file"],
                             arm="tooled", trial_idx=0,
                             chat_client=_StubChat(), model="stub")
        self.assertIsNone(t.error)
        self.assertGreaterEqual(t.tool_calls, 1)

    def test_stub_verdict_catches_nothing(self):
        """Guard on the dry run itself: if the stub's canned text ever
        drifts into wording that trips the oracle, a wiring check would
        start reporting recall."""
        for arm in ("untooled", "tooled"):
            t = run_detect_trial(self.qs["easy-01-fabricated-file"], arm=arm,
                                 trial_idx=0, chat_client=_StubChat(),
                                 model="stub")
            self.assertEqual(t.caught, [])
            self.assertEqual(t.false_positives, [])

    def test_a_planted_falsehood_is_scored_when_the_verdict_names_it(self):
        """End-to-end proof the oracle fires at all — otherwise every
        0% above is indistinguishable from a dead scorer."""
        q = self.qs["easy-01-fabricated-file"]
        chat = _StubChat(text=("Claim 2 cites retry_state.py; that file "
                               "does not exist in this package."))
        t = run_detect_trial(q, arm="untooled", trial_idx=0,
                             chat_client=chat, model="stub")
        self.assertEqual(t.caught, ["retry_state.py"])
        self.assertEqual(t.missed, [])

    def test_node_exception_is_recorded_not_raised(self):
        """One bad trial must not abandon the cohort mid-run."""
        class Dead:
            def chat(self, payload):
                raise RuntimeError("upstream down")

        t = run_detect_trial(self.qs["easy-02-wrong-constant"],
                             arm="untooled", trial_idx=0,
                             chat_client=Dead(), model="stub")
        self.assertIsNotNone(t.error)
        self.assertIn("upstream down", t.error)
        self.assertEqual(t.caught, [])

    def test_control_question_yields_no_planted_false(self):
        t = run_detect_trial(self.qs["medium-02-all-true"], arm="untooled",
                             trial_idx=0, chat_client=_StubChat(),
                             model="stub")
        self.assertEqual(t.caught, [])
        self.assertEqual(t.missed, [])


if __name__ == "__main__":
    unittest.main()
