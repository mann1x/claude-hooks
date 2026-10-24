"""The coder suites tell the coder what they grade, and every oracle loads.

2026-10-24: coder_mlang kept each question's API, constraints and output
format in the markdown body, and the harness sent only the one-line
``task`` — so the oracles graded rules no model was shown (minimax named
``min_conference_rooms`` after the task's wording; the spec says
``min_meeting_rooms``). Two of its oracles also failed at collection
(``name 'pytest' is not defined``), so no model could ever pass them.
These run with no model and no toolchain.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "benchmarks" / "consultants"))

import harness  # noqa: E402

QUESTIONS = _REPO / "benchmarks" / "consultants" / "questions"
SUITES = ("coder", "coder_easy", "coder_med", "coder_mlang")


def _api_names(body: str) -> set[str]:
    return set(re.findall(r"\b(?:def|fn|func|function|class|struct)\s+(\w+)",
                          body))


@pytest.mark.parametrize("q", harness.load_questions(QUESTIONS / "coder_mlang"),
                         ids=lambda q: q.id)
def test_the_mlang_coder_is_shown_the_spec_it_is_graded_on(q):
    assert q.body and q.body.split("\n", 2)[-1].strip()[:200] in q.task
    for name in _api_names(q.body):
        assert name in q.task, (q.id, name)
    # The one-line summary still leads.
    assert len(q.task) > len(q.body) - 200


@pytest.mark.parametrize("suite", ("coder", "coder_easy", "coder_med"))
def test_other_suites_send_their_task_unchanged(suite):
    for md in sorted((QUESTIONS / suite).glob("*.md")):
        if md.name in ("SUITE.md", "README.md", "NOTES.md"):
            continue
        fm, body = harness._parse_frontmatter(md.read_text(encoding="utf-8"))
        assert "spec" not in fm, md
        assert harness.task_of(fm, body) == (fm.get("task") or "").strip(), md


def test_task_of_appends_the_body_only_when_asked():
    body = "\n# q-1\n\nUse `f(x)`.\n\n```py\nclass A:\n    pass\n```\n"
    assert harness.task_of({"task": "Do it."}, body) == "Do it."
    joined = harness.task_of({"task": "Do it.", "spec": "body"}, body)
    assert joined.startswith("Do it.\n\nUse `f(x)`.")
    assert "class A:\n    pass" in joined          # indentation kept
    assert "# q-1" not in joined                    # the title is not spec
    assert harness.task_of({"spec": "body"}, body).startswith("Use `f(x)`.")


@pytest.mark.parametrize("suite", SUITES)
def test_every_oracle_loads(suite, tmp_path):
    (tmp_path / "solution.py").write_text("")
    oracles = sorted(str(p) for p in (QUESTIONS / suite).glob("*-oracle.py"))
    assert oracles
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "--collect-only", "-q", *oracles],
        cwd=_REPO, capture_output=True, text=True, timeout=300,
        env={**os.environ, "CODER_SANDBOX": str(tmp_path)})
    assert r.returncode == 0 and " error" not in r.stdout, r.stdout[-2000:]
