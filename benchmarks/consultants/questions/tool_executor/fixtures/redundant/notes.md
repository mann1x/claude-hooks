# notes.md — redundant cohort

This file exists only so the fixture directory isn't empty.

The bench question in `medium-02-redundancy-test.md` deliberately
embeds the answer in its `why` block — a well-calibrated
tool_executor should recognise it doesn't need to read this file
at all. The oracle penalises any model that issues read_file
against `notes.md`.
