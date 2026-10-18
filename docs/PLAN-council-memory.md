# Council memory and a critic that can check

Status: M1 and M2 done (2026-10-18); M3 next. Owner decisions are marked **(decided)**.

## Why

Five graded councils on the same question (cline-sync's tool surface,
mails #1625–#1641) found real defects, then lost them. Every council
starts cold:

- run 3 found grep stamp laundering and the apply_patch move-onto
  defect; neither came back in runs 4 or 5;
- run 4 found the surrogate split; run 5 did not re-check it;
- for four runs the critic ranked the read ledger "the biggest gap",
  though it is unwired on purpose and says so in a comment no lane read;
- run 5's critic called grep's full-file read credit a defect; it is
  deliberate.

The store's `("project", id)` namespace does not fix this. It holds
summaries the distiller writes when old research expires: fuzzy,
undated as to truth, never decided by anyone.

The critic, meanwhile, can read code but not run it. When it doubts a
report, the only move it has is "needs more research", which sends the
whole council back for another round with nothing more than its prose.

## Decisions

1. **(decided)** The council gets memory: a ledger of findings per
   project.
2. **(decided)** A finding is settled only by a decision of the user or
   the assistant reviewing the council, never by the council itself.
3. **(decided)** No "deliberate, don't flag" list for the critic. A
   finding settled *by design* is handed to the critic with its reason;
   the critic may still challenge it, with evidence against that reason.
4. **(decided)** The critic can run commands: if the lanes can, the
   critic can, so it can check a doubt itself before it sends anyone
   back.
5. **(decided)** The critic may cross-examine a lane it is not convinced
   by, instead of only sending everyone back.

## The ledger (M1)

A finding is a claim with a verdict. A task is work to do. Mixing them
would blur both, so the ledger is its own record, built the way tasks
are: the files are the record, an index is only for search.

```
<project>/.claude-hooks/findings/
  config.toml      prefix = "csf", project = "cline-sync"
  csf-1.md         one file per finding
```

The prefix is the project's task prefix plus `f`, derived once and
written down (`derive_prefix`), so `csf-12` never collides with a task
id.

### A finding file

```markdown
---
id: csf-3
title: apply_patch create, then edit refused as unread
status: confirmed
severity: H
paths: [sdk/packages/core/src/extensions/tools/executors/apply-patch.ts:412]
symbols: [executeApplyPatch]
councils: [csl-2026-10-18-1358-a02b, csl-2026-10-18-1458-cb89]
task: ""
commit: ""
settled_by: user
settled_at: 2026-10-18T15:02:11Z
created: 2026-10-18T14:30:00Z
updated: 2026-10-18T15:02:11Z
---

## Claim
After apply_patch creates a file, the next editor call is refused as
"not read".

## Reproduce
cd sdk/packages/core && bun x vitest run --configLoader runner src/…/audit3.test.ts

## Evidence
- 2026-10-18 csl-…-cb89 C212 (researcher lane 0): exit 1, "refused: read the file first"

## Decision
Confirmed by re-run on ac1ee35c56 (ollama's grade, mail #1641).

## Log
- 2026-10-18 15:02Z settled confirmed by user: …
```

### Statuses

| status | meaning | who sets it |
|---|---|---|
| `candidate` | a council said so; nobody has decided | the council (recorded after a run), or anyone |
| `confirmed` | the defect is real | user / assistant |
| `refuted` | it is not; the decision names the run or reason | user / assistant |
| `by_design` | the behaviour is intended; the decision says why and where | user / assistant |
| `fixed` | was confirmed, now fixed; `commit` is required | user / assistant |

`settle()` refuses a decision without a reason, a `by` other than
`user` / `assistant`, or `fixed` without a commit. A settled finding
returns to `candidate` only through `reopen()`, with a reason, by the
same two parties. The engine has no code path that settles.

### Matching

A council's new claim is matched against the ledger before it becomes a
new candidate: path overlap (same file, line within a window) plus
claim-word overlap (stemmed content words, as `is_about_the_search`).
A match adds evidence and the council's id to the existing finding
instead of a duplicate file. `relevant(question, paths, k)` serves M2.

A project ledger holds tens of findings, so M1 matches in Python over
the files. An embedding index on the host store (as `tasks`) is added
when a ledger outgrows that.

### Surfaces

- `claude_hooks/findings/` — model, ledger, matching (no engine import).
- MCP tools on both memory servers (`finding-list`, `-show`, `-record`,
  `-settle`, `-reopen`, `-note`), off with `hooks.findings.enabled:
  false`.
- `claude-consultants findings list|show|settle|reopen|note`.

## M2 — the council reads and writes the ledger

- The planner gets the relevant findings, by status:
  - `confirmed` (not fixed): re-verify with its reproduction, do not
    rediscover;
  - `fixed`: check that the fix holds;
  - `refuted`: do not re-raise without new evidence; here is the run;
  - `by_design`: here is the intent; challenge it only with behaviour
    that contradicts it.
- The synthesizer tags each ranked item `known (csf-3, confirmed)` or
  `new`.
- After a council, its ranked items and matrix rows that cite a run
  are recorded as candidates (or as evidence on a match), with C<n>,
  printed lines and the command. Items that cite no run are not
  recorded.
- The review loop (skill) lists the run's candidates and proposes
  settlements; the user decides.

## M3 — the comment at the cited line

Every `path:line` a report cites reaches the critic and meta-critic
with the comment block around it (the contiguous comment lines above
and at the line, and a docstring when the line is a definition),
taken in code, no model call. The read ledger's "unwired on purpose"
comment is what four critics missed.

## M4 — the critic runs commands

On a shell run the critic gets `run_command`, through the same sandbox
backend, policy gate and session store mask as the lanes:

- read-only shell: in the session's read-only view;
- `--shell write`: its own view (lane `critic:<n>`), like the coder's,
  so nothing it writes reaches a lane or the user unless held and
  applied like any lane's patch;
- its runs are in `transcript.db` as `tool_call` rows with role
  `critic`, so they get C<n> ids and the claim audit and Dropped rules
  apply to them as to any run;
- the critic's own loop budget is bounded like a lane's
  (`researcher_iters`-style shell bonus);
- the evidence hold counts a critic run that reached the code as a run;
  a critic `ready` still needs one.

`LANE_ONLY_TOOLS` stops being "lanes only": the set of roles that may
run commands becomes explicit (`SHELL_ROLES` = researcher,
tool_executor, coder, critic), and the synthesizer and planner stay
without.

## M5 — cross-examination

When the critic is not convinced, it may issue challenges instead of a
blanket `needs_more_research`:

```
CHALLENGE lane=7: S2e calls the edit of line 5 BLIND, but C58 shows
line 5 read and unchanged. Settle with: a run that edits a read,
unchanged line and shows whether it is refused.
```

- each challenge names the lane, the claim and what would settle it;
- the challenged lane resumes its own conversation
  (`load_role_messages`) with the challenge as a harness note, and must
  answer with a run or a concession; one exchange, tools on, bounded
  turns; challenged lanes run in parallel;
- the critic rules on each: `upheld` (the claim falls), `withdrawn`
  (the lane's run answered it), or `open`;
- an `open` challenge falls back to today's reroute, so nothing is lost;
- a lane re-raising a `refuted` or `by_design` finding is challenged
  against the recorded decision.

Behind `[critic] cross_examination = on|off` (default off) until a
graded run shows it pays.

## Order and tests

M1 → M2 → M3 → M4 → M5. Each milestone ships with tests and a mutation
check per new rule, and is deployed only between graded runs.
