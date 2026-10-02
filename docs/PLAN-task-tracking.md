# Plan: persistent task tracking

Status: **approved 2026-10-02**, decisions below. Being built.

## The problem, measured

Long sessions lose track of their tasks, their status and the plans
behind them. The backup_models session is the clearest case.

- **Session `4208dc59`** has run since 2026-04-08. Its transcript is
  4.2 GB and has been compacted 735 times.
- **It made 2,513 task-tool calls** (852 `TaskCreate`, 1,647
  `TaskUpdate`), reaching task #856.
- **The tasks were rich.** The descriptions were used as running logs
  (results, scores, decisions), and 60 tasks carried dependencies.
- **None of them was ever on disk.** `~/.claude/tasks/` has no list for
  that session. The only record is the transcript, so "restore the
  tasks" means digging through 735 compactions.
- **Use stopped dead on 2026-08-26.** The next day the session moved
  from Claude Code 2.1.220 to 2.1.247. Since then, the task tools are
  not offered to newer models unless `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`
  is set ([tools reference](https://code.claude.com/docs/en/tools-reference#task-tool-availability)).
  This session (Opus 5.5, 2.1.284) has no `TaskCreate` either. The
  session didn't forget its tasks; it lost the tools.
- **The list can be rebuilt.** Replaying the calls from the transcript
  gives 849 tasks: 671 done, 96 pending, 5 in progress and 77 deleted.
  That is the migration input.
- **Claude Code's own list is not a system of record.** It deletes
  completed tasks, even on named lists
  ([#78147](https://github.com/anthropics/claude-code/issues/78147),
  closed "not planned"). The task tools have also disappeared from the
  model before
  ([#80015](https://github.com/anthropics/claude-code/issues/80015)).
- **csb (Claude-Session-Backup) never backed anything up.** There is no
  git repository in `~/.claude`, so it indexed 0 sessions. Claude Code
  also deletes transcripts 30 days after their last use
  (`cleanupPeriodDays`), so the one record of those tasks is only safe
  while the session stays in use.

## Requirements

1. **pgvector is the source for recall.** Tasks surface through the
   hooks the way memory and mail do, on every host.
2. **Plain local files** that a person can read, review and edit.
3. **Linux and Windows by default.** Every host that installs
   claude-hooks gets it, with no per-host setup.
4. **Can't be forgotten.** Open tasks are pushed into context; the model
   never has to remember that a list exists.
5. **A task carries what is behind it:** plan, files, commits, decisions
   and the log of what happened.
6. **Negligible idle cost**, like the mailbox relay.

## Options considered

From the research report (sources in its links):

| Candidate | Verdict |
|---|---|
| **Taskwarrior 3** | Excellent data model (UDAs, annotations, `depends`, urgency, JSON export). The v3 store is a SQLite replica, not readable files, and the CLI has no native Windows build. **Take the model, not the backend.** |
| **beads (`bd`)** | Best agent ergonomics (short ids, `ready`, `claim`, dependencies). The source of truth is an embedded Dolt database and the files are only an export; it also has a memory of its own. **Take the verbs.** |
| **Backlog.md** | One Markdown file per task, with YAML front matter and Description / Acceptance `- [ ]` / Plan / Notes sections. Closest to "plain files". It is a Node tool with no Postgres side. **Take the file format.** |
| task-master-ai, Shrimp, git-bug, todo.txt, Org | Wrong license, stale, unreadable storage, too little structure, or too niche. |
| Claude Code Task tools | Off by default on current models, garbage-collected, session-scoped. **Use as an input only.** |

**Decision:** build it natively in claude-hooks (stdlib core, same
pattern as the mailbox). Use Backlog.md's file format, Taskwarrior's
data model and beads' verbs.

## Design

### Where the files live

Per project, inside the repository:

```
<project>/.claude-hooks/tasks/
  config.toml        prefix = "bm" (written once)
  bm-42.md           one file per task
  archive/bm-7.md    closed tasks
<project>/TASKS.md   generated board: active, ready, blocked, recently done
```

`TASKS.md` is regenerated whenever a task changes. It is the page a
person opens; it is never edited by hand, and its header says so.

### The task file

```markdown
---
id: bm-42
title: "R9.run3: GEPO brevity with the MoE router in LoRA scope"
status: active            # pending | active | waiting | done | cancelled
priority: H               # H | M | L
area: R9.gepo             # dotted project path, Taskwarrior-style
tags: [gepo, coderx]
depends: [bm-41]
plan: docs/plans/plan_r9_gepo_a3b_coderx.md
links: {commits: [2f01f528b], files: [scripts/gepo_brevity.py], mail: [614]}
created: 2026-08-26T18:50:00Z
updated: 2026-08-26T19:33:00Z
sessions: [4208dc59]
---

## Description
What and why, kept current (rewritten in place).

## Acceptance
- [ ] router LoRA targets present in both ranks
- [x] smoke gates pass

## Log
- 2026-08-26 19:33 [4208dc59] RUNNING on bs2, both GPUs, ~18-20h.
- 2026-08-26 18:50 [4208dc59] created.
```

The rules for each part:

- **Front matter: changed only through the tools.** Anthropic's
  long-running-agent guidance found models overwrite free Markdown more
  readily than structured fields, so status is never edited as prose.
- **Description and Acceptance: rewritten in place.**
- **Log: append-only**, one timestamped line per event, newest first.
  This replaces the habit of stuffing results into the description.
- **Parsing:** the front matter is a restricted YAML subset (scalars, flow
  lists and maps), parsed with the stdlib. No PyYAML.

### pgvector (and sqlite_vec)

A `tasks` table on the store the host already uses, borrowed the way the
mailbox borrows it:

- **Key:** `(project_id, id)`.
- **Columns:** every front-matter field, plus `body`, `file_path`,
  `file_hash`, `host`, an embedding of title + description + latest log
  lines, and a full-text vector.
- **Who reads it:**
  - recall: semantic and hybrid search;
  - cross-host and cross-project views ("what is open in backup_models",
    from any session);
  - the hooks.
- **Which side wins:** the file is the record and the row is its index.
  Tool writes update both. On SessionStart, and before any task tool
  call, files whose mtime or hash changed are re-read into the row. So a
  hand edit wins, and the check is a stat of one small directory.

### The surface

**MCP tools** on the existing pgvector server, named the way models
expect:

| tool | does |
|---|---|
| `task-create` | title, description, area, priority, depends, plan, acceptance |
| `task-ready` | what can be worked on now: open, unblocked, by urgency |
| `task-list` | filters (status, area, tag, query, since); pages like the mailbox |
| `task-show` | the whole task, plus a summary of its linked plan, files and commits |
| `task-start` / `task-done` / `task-wait` / `task-cancel` | status changes, each with an optional log line |
| `task-note` | append a log line |
| `task-update` | edit fields, description or acceptance |
| `task-link` | attach a commit, file, mail, consultancy or plan |

**CLI** for people: `claude-hooks-tasks list|ready|show|add|done|note|board|import`.

**Urgency:** Taskwarrior's formula, simplified. It weighs priority, age,
blocking others, being blocked, active status and a nearby due date. It
orders `task-ready` and the injected list.

### Hooks: making it impossible to forget

- **SessionStart, including after `/clear` and compaction.** Inject a
  short block:
  > **Tasks (backup_models):** 3 active · 12 ready · 4 blocked —
  > bm-854 R9.run3 (active, 5 d) · bm-856 R9.replay-wire (ready, H) · …
  > Full board: `TASKS.md` · use `task-ready` / `task-show`.

  After compaction this is the fix for "restore the tasks".
- **UserPromptSubmit:**
  - If the prompt names a task (`bm-42`, `task 42`), inject that
    task's summary.
  - Task rows join the recall search, weighted towards active ones.
- **Stop (nudge, once):** if a turn changed files linked to an active
  task, or ran for N turns with an active task never touched, ask once
  to update it. Same pattern as the mail nudge: once per change, never
  on a continuation.
- **PreCompact:** write the active and ready tasks into the wrap-up, so
  the compacted context carries them.
- **Claude Code's own tasks as input:**
  - `TaskCreated`, `TaskCompleted` and PostToolUse on `TaskUpdate` are
    imported into this store.
  - A session where the built-in tools exist (old models, or
    `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`) therefore still ends up here.
  - Nothing depends on them.

### Plans

Plans stay what they are: Markdown under the project (for backup_models,
`MASTER_PLAN.md` and `docs/plans/*.md`). A task links to its plan with
`plan:`. `task-show` summarises the linked plan, and `TASKS.md` groups
tasks under the plans they belong to. A plan's own file is never
rewritten by the system.

### Migration

`claude-hooks-tasks import --transcript <jsonl>` replays
`TaskCreate` / `TaskUpdate` calls into task files:

- **Statuses:**
  - `completed` → done, straight to `archive/`.
  - `deleted` → cancelled, also to `archive/`.
  - `in_progress` → active.
  - `pending` → pending.
- **Data:**
  - The old description becomes Description.
  - Each later update becomes a Log line.
  - `addBlockedBy` becomes `depends`.
- **Ids:** kept, with the prefix (`bm-856` for #856), so references in
  old notes still resolve.

For backup_models: 849 tasks, of which 101 are open.

## Milestones

1. **Store and files.** Task model, front-matter parser, file writer,
   `tasks` table (pgvector + sqlite_vec), reconciliation. Tests on both
   dialects and both OSes.
2. **Tools.** MCP tools on pgvector-mcp / sqlite-vec-mcp, plus the CLI.
   Urgency and `ready`.
3. **Hooks.** SessionStart block (all sources), prompt mentions, recall
   integration, PreCompact, Stop nudge, import from built-in task events.
4. **Board and plans.** `TASKS.md` generation, grouping by plan.
5. **Migration.** Transcript importer, run on backup_models after a
   dry run you review.
6. **Install, deploy and docs.** On by default with the hooks on every
   OS; deploy and verify coverage; runbook.

## Decisions (2026-10-02)

1. **Files:** `.claude-hooks/tasks/`, with a generated `TASKS.md` board
   at the project root.
2. **Ids are prefixed by project:** `bm-42` for backup_models, so an id
   is unique across projects in mail, notes and recall. The prefix is
   derived once (initials of the project name: `backup_models` → `bm`),
   stored in `.claude-hooks/tasks/config.toml`, and never re-derived.
3. **Import all 849 backup_models tasks**, done and cancelled ones
   straight to `archive/`, renumbered with the prefix (`#856` → `bm-856`).
4. **Stop nudge: on** by default.
5. **No stopgap.** The built-in task tools stay off; backup_models'
   tasks come over through the importer.
