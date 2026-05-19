"""Empirical benchmarks for the /consultants v2 council.

Three sub-milestones land here (per the v2 overhaul plan, M11):

- M11a (``stall_bench``): GPQA-Diamond-style hard questions; measures
  inter-token cadence to set per-model ``stall_threshold_s`` and
  ``hard_cap_s`` defaults (shipped conservatively in M3).
- M11b (``coder_bench``): HumanEval-style code-gen tasks; picks the
  default model for the M10 ``coder`` role.
- M11c (``tool_executor_bench``): multi-tool research tasks against
  the live codebase; decides whether to flip the M6 ``tool_executor``
  default-on, plus whether x-tier composition is worth the cost
  (gates task #103).

Each runner shares ``harness.py`` so the question schema, trial
record format, cost estimator, and report renderer stay consistent.
"""
