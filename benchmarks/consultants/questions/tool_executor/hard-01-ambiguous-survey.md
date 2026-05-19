---
id: hard-01-ambiguous-survey
tier: hard
source: synthetic
task: |
  How is serialization done in this codebase? List every distinct
  serialization approach you find and give one sentence on what
  each one is for. Cite the files where each approach lives.
why: "Researcher needs a serialization map before scoping a wire-format migration"
suggested_tools:
  - survey_project
  - list_files
  - glob
  - read_file
oracle: hard-01-ambiguous-survey-oracle.py
fixtures_subdir: serializers
---

# hard-01: ambiguous survey

No file path is given. The model must discover the cohort — a
package with four files (`json_codec.py`, `binary_codec.py`,
`csv_codec.py`, `router.py`) — and report on the three codecs
plus the router. The intended path is survey_project (or
list_files) → glob/list_files → read the codecs.

The oracle checks discovery breadth: a passing answer mentions
JSON, binary, and CSV — leaving out CSV is the most common
failure mode (it's the smallest file). The router file is bonus
but not required.
