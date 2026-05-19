"""pytest config for the coder_mlang oracle suite.

Registers the ``constraint`` / ``algorithm`` markers used by the
two-axis grading (introduced 2026-05-17 alongside the v1.0.1
oracle redesign).

- ``@pytest.mark.constraint`` — structural / source-grep checks
  (function names, banned imports, type declarations). These do
  NOT decide algorithmic correctness; they measure
  instruction-following. The harness runs them but does not gate
  ``trial.passes_algorithm`` on them.

- Default (no marker) — algorithmic tests that compile + run the
  produced binary and assert on stdin/stdout. These decide
  ``trial.passes_algorithm`` and the report's pass-rate column.

Why this matters: the v1.0 cohort run (90 trials) had the
harness invoke pytest with ``-x`` (exit on first failure), which
meant a single source-grep miss aborted the run before any
algorithmic test got to execute. v1.0.1 drops ``-x``, lets every
test run, and splits the results.
"""

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "constraint: source-grep / structural check; does NOT "
        "gate trial.passes_algorithm",
    )
    config.addinivalue_line(
        "markers",
        "algorithm: behavior test on the produced binary; "
        "gates trial.passes_algorithm",
    )
