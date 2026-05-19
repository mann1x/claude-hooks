# Forensic — researcher-side fabrication chain — 2026-05-18

Forensic completion for the two open questions left after the M14
[first-real-ask post-mortem](rerun-report.md):

- **Task #201 (was task #1)**: was gemma4:31b-cloud's wrong-line
  citation a synthesizer hallucination, a researcher misinterpretation,
  or grounded-but-wrong attention?
- **Task #202 (was task #2)**: was glm-5.1:cloud's fake-filename
  citation in `csl-2026-05-18-1031-9e3b` a one-off, a prompt artifact,
  or a class-level researcher failure mode?

Both answers proved the existing M14 CitationLinter is hooked at the
wrong layer. The fix (#204) ships in this commit.

## Task #201 — gemma synthesizer "wrong line" forensic

Session: `csl-2026-05-18-1156-115c`. Final answer cited
`consultants/engine/store_reaper.py:301` as the location of
`_distill_group` (real definition at line 360), plus four more
cites in the same shape (302/313/392/416).

### Provenance trace

Grepped `events` table for every model output that mentioned the
fabricated line numbers (301, 302, 313, 392, 416):

```
researcher llm_calls: 39
researcher events mentioning target lines: 14
  event_id=199 gemini-3-flash-preview:cloud lane=0 lines=[290, 296, 301, 302, 303, 310, 313, 416]
  event_id=203 gemini-3-flash-preview:cloud lane=3 lines=[290, 301, 302, 303, 310, 313, 392, 416]
  event_id=213 gemma4:31b-cloud lane=1 lines=[301, 302, 310, 313, 392]
  event_id=253 gemma4:31b-cloud lane=4 lines=[301, 302, 310, 313, 416]
  event_id=267 gemma4:31b-cloud lane=7 lines=[290, 301, 302, 303, 313]
  (+9 more across lanes 0/3/6/7)
```

The line numbers **originate in researcher REPORTs**, not the
synthesizer. The synthesizer (`gemma4:31b-cloud`) faithfully
relayed numbers that 14 different researcher outputs (across
**both** `gemini-3-flash-preview` and `gemma4:31b-cloud`)
already contained.

### Where did the researchers get those numbers?

Cross-checked: searched the `output` field of every `tool_call`
event that touched `store_reaper.py`. Result:

```
events with store_reaper.py in tool output: 30
  event_id=56 grep matches=[301, 302, 313, 392, 416]
  event_id=70 grep matches=[301, 302, 392]
  event_id=72 grep matches=[313, 416]
  event_id=76 grep matches=[313, 416]
  ... (5 more grep events with subsets)
all target lines seen in tool output: [301, 302, 313, 392, 416]
```

**Every "fabricated" line is REAL.** They are grep hits at literal
file:line positions where the patterns `delete|distill` matched
inside `sweep_once`'s body (lines 245-334) — at the CALL sites
where `sweep_once` invokes `self._distill_group(...)`,
`self._write_summary(...)`, `self._delete_rows(...)`. Sample from
real source:

```
295  try:
296      deleted += self._delete_rows(rows)
297  except Exception:
298      log.exception("store-reaper: delete failed for sid=%s", sid)
299  continue
300  try:
301      summary = self._distill_group(sid, rows)   ← grep hit
302      self._write_summary(sid, rows, summary, distilled_at=now)
```

### Conclusion #201

The synthesizer is **innocent**. The failure mode is **researcher
misinterpretation of grep output**: the researcher conflated "line
where pattern matched" with "line where function is defined". Both
`gemma4:31b-cloud` and `gemini-3-flash-preview:cloud` did this in
this session — it is not gemma-specific.

The CitationLinter's symbol-mismatch layer caught all five in the
synthesizer output anyway (the answer shipped with inline
`[in sweep_once, not _distill_group]` markers), so the user was
never misled.

**No model swap warranted.** The original hypothesis ("gemma's
training-data prior is winning over the prompt directive")
was wrong.

## Task #202 — glm-5.1 fake filename forensic

Session: `csl-2026-05-18-1031-9e3b`. Final answer cited
`consultants/engine/store_sql.py:41-61` — a file that does not
exist anywhere in the repo.

### Provenance trace

Searched the entire `events` table for "store_sql":

```
events involving store_sql: 3
  ev=341 researcher  llm_call  glm-5.1:cloud           lane=5  in_req=0 in_resp=1
  ev=348 critic      llm_call  gemini-3-flash-preview  lane=-  in_req=1 in_resp=0
  ev=351 synthesizer llm_call  gemma4:31b-cloud        lane=-  in_req=1 in_resp=1
```

- **Event 341 (origin)**: `glm-5.1:cloud` researcher lane 5.
  `in_req=0, in_resp=1` — the prompt did NOT contain "store_sql",
  but the response did. Pure invention.
- **Event 348 (critic)**: gemini-3-flash saw it in research
  aggregation, did not echo it forward.
- **Event 351 (synthesizer)**: gemma4 saw it AND emitted it.

### Worst-class: lane 5 had ZERO tool calls

```
lane 5 total events: 18
  ev=  5 node_enter
  ev= 13 llm_call  glm-5.1:cloud
  ev= 14 node_exit
  ev=266 node_enter
  ev=269 node_enter
  ... (5 more node_enters)
  ev=292 llm_call  glm-5.1:cloud
  ev=309 llm_call  glm-5.1:cloud
  ev=314 llm_call  glm-5.1:cloud
  ev=329 llm_call  glm-5.1:cloud
  ev=341 llm_call  glm-5.1:cloud          ← fabrication origin
  ev=342 node_exit
```

Zero `tool_call` / `tool_result` events. glm-5.1 emitted 6 LLM
turns in REPORT mode (under M11c-3 fanout) WITHOUT ever invoking
grep or read_file. The "source listing" in event 341's response
is therefore 100% fabricated. Sample:

```
21  
22  from .distiller import Distiller, DistillationFailed
23  from .store_sql import StoreSQL          ← invented import
24  from .transcript_db import TranscriptDB   ← invented module
25  
... 
@dataclass
class ReaperConfig:                            ← invented class
    sweep_interval: float = 3600.0            ← invented fields
    min_age_hours: float = 24.0
    enable_distillation: bool = True
    batch_size: int = 50
    max_concurrent_distills: int = 3
```

The real code uses a `StoreDistillationConfig` dataclass from
`consultants/config.py` with entirely different field names. glm
generated a plausible-looking module structure from prior pattern
recognition.

### Retroactive lint — what would have caught this

Ran the CitationLinter (commit `159d353`) against event 341's
response text:

```
issues caught: 17
unique fabricated paths:
  - consultants/engine/distiller.py        (fake)
  - consultants/engine/store_reaper.py     (real, wrong lines)
  - consultants/engine/store_sql.py        (fake)
  - consultants/engine/transcript_db.py    (fake)
```

The linter would have caught all 17 fabrications IF it were
wired at the researcher boundary. It was wired only at
synthesizer output, so the contamination flowed freely through
peer_findings → critic → synthesizer.

### Conclusion #202

`glm-5.1:cloud` in researcher role can fabricate entire fake
modules with zero grounding. The fabrication propagated through
all downstream roles because **citation verification was only
running at the very last step**.

The fix is structural, not roster: wire the linter at the
researcher boundary so annotated cites flow into peer_findings.

## Fix — #204 — researcher-side citation lint

`consultants/engine/council.py` gains a `_lint_research_text`
closure inside `researcher_node`. Three exit sites (M6 REPORT,
PLAN-mode empty fallback, v1 inline-loop) now run the linter
on the researcher's REPORT before the text flows into the
`research` field of the return dict.

Key design choice: **annotate the downstream-visible text only,
leave the `turn` record raw**. Rationale: the transcript should
stay a faithful "what the model said" forensic — operators
investigating future hallucinations need the raw output. The
downstream propagation path (peer_findings, store,
synthesizer's input) gets the annotated form so every other
role sees the linter's verdict alongside the claim.

The synthesizer-side lint stays as belt-and-suspenders for
cites the synthesizer introduces or transforms.

### Net effect on csl-2026-05-18-1031-9e3b (hypothetical replay)

- Researcher lane 5 emits the fake source listing →
  citation_linter annotates 17 cites in-place →
  the annotated form (now 17 `[unverified — file not found]` markers
  inline) is what enters peer_findings.
- Other lanes see the annotated form and (per the existing prompt
  block "treat unverified cites as suspect") would not reinforce.
- Critic sees the annotated form — no need to invent a new
  "flag ungrounded cites" prompt; the annotation already does it.
- Synthesizer sees pre-annotated research and the
  synthesizer-side lint catches any residual fabrication it
  invents.

### Tests

- `tests/test_consultants_council.py::TestResearcherCitationLint` (new, 3 tests):
  - Fabricated cite annotated in `research` field, not in the
    turn record.
  - Empty `cwd` → no allowed_roots → no annotation (defensive).
  - Real cite passes through unchanged.

Both env full sweep: 3811 + 3713 passing (+3 each, no regressions).
