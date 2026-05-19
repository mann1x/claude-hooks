---
id: easy-02-listfiles-glob
tier: easy
source: synthetic
task: |
  List every `*.py` file under the `pkg/` subdirectory, then
  describe at least three of them in one sentence each (what
  the module is for). Cite each described module by its file
  path.
why: "Researcher needs a quick package-shape map before drilling in"
suggested_tools:
  - list_files
  - glob
  - read_file
oracle: easy-02-listfiles-glob-oracle.py
fixtures_subdir: multipkg
---

# easy-02: list + glob + describe

A package-tour task. The model must discover the modules
(`__init__.py`, `parser.py`, `runner.py`, `serializer.py`,
`validator.py`), read at least three of them, and produce a
one-sentence summary per module. The oracle checks both file
discovery (every name appears) and content fidelity (at least
three of the four non-init modules get plausibly correct
descriptions).
