---
id: medium-01-multifile-audit
tier: medium
source: synthetic
task: |
  Find every TODO comment in this codebase and report them with
  file + line citations. Group them by their tag prefix
  (`TODO(api)`, `TODO(cache)`, `TODO(auth)`) so the researcher
  knows which subsystem each one targets.
why: "Researcher needs a tech-debt audit across the demo service before scoping a refactor"
suggested_tools:
  - grep
oracle: medium-01-multifile-audit-oracle.py
fixtures_subdir: todos
---

# medium-01: multi-file TODO audit

Three-file cohort with five TODOs total (api: 2, auth: 2,
cache: 1). The intended path is a single grep over the cohort,
NOT per-file reads — a model that opens each file in turn
will waste tokens. The oracle checks coverage (all five tag
prefixes named) and citation density (at least 3 distinct
`file:line` references).
