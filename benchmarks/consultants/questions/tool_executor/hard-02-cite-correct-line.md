---
id: hard-02-cite-correct-line
tier: hard
source: synthetic
task: |
  In `settings.py`, what is the current value of `MAX_BATCH_SIZE`
  and on what exact line is it set? Cite the location as
  `settings.py:LINE`. The historical / old value in the comment
  does NOT count — the answer must point at the live assignment.
why: "Researcher needs to confirm the active batch size before sizing the executor pool"
suggested_tools:
  - read_file
  - grep
oracle: hard-02-cite-correct-line-oracle.py
fixtures_subdir: configdrift
---

# hard-02: cite the exact line

A precision test. The fixture has two `MAX_BATCH_SIZE` mentions
near each other:

- A `# Historical note: MAX_BATCH_SIZE used to be 100` comment.
- The live assignment `MAX_BATCH_SIZE: int = 250`.

A model that returns the comment line (or worse, the value
`100`) fails. The oracle resolves the live-assignment line
dynamically against the fixture so reflowing the file by adding
header comments later doesn't break the test — only the
relationship "the cited line is the live assignment, not the
comment" is checked.
