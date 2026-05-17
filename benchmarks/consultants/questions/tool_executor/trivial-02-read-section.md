---
id: trivial-02-read-section
tier: trivial
source: synthetic
task: |
  Read the README.md in this directory and summarize the
  "Configuration" section. Include the names of every environment
  variable the section documents and their default values. Cite
  the file as `README.md`.
why: "Researcher needs the env-var surface before recommending a config tune"
suggested_tools:
  - read_file
oracle: trivial-02-read-section-oracle.py
fixtures_subdir: readme_basic
---

# trivial-02: read a specific section from a markdown file

A single-file read task where the model must locate the
Configuration section among others (Overview, Installation,
Usage, Logging, License) and return its contents. The oracle
checks that all three env-var names + their defaults appear in
the answer and that the model did NOT bleed material from other
sections (which would inflate the response and miss the
"specific section" intent).
