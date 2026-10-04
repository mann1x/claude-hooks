# Task tracking

A persistent task list per project, kept in plain files and indexed in
the memory store, pushed into every session by the hooks. Design and
the measurements behind it: [`PLAN-task-tracking.md`](PLAN-task-tracking.md).

**Why it exists.** Claude Code's own task tools are not a record. They
are not offered to current models unless `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`
is set, completed tasks are garbage-collected, and the list lives in the
session. backup_models made 2,513 task calls over four months. When its
session moved to Claude Code 2.1.247 on 2026-08-27 the tools disappeared,
and the 849 tasks survived only in a 4.2 GB transcript.

## Where things are

```
<project>/.claude-hooks/tasks/
  config.toml        project = "backup_models", prefix = "bm"
  bm-42.md           open tasks (pending / active / waiting)
  archive/bm-7.md    done and cancelled ones
  .gitignore         in-flight write locks, and state files' old names
<project>/TASKS.md   the board, regenerated on every change
~/.claude/claude-hooks-tasks/<project>-<hash>/
  .index-state.json  reconcile cache: (mtime, size) per task file
  .nudge-<sid>.json  which Stop nudges a session has had
  .cc-<sid>.json     Claude Code task ids → ours, for the mirror
```

**Per-host state lives under `~/.claude`, not in the task folder.** None
of it is a task, and the hook daemon cannot write project folders: its
unit runs with `ProtectSystem=strict` and only `~/.claude` writable, so
the reconcile cache failed with `Read-only file system` on every write
there. The directory is keyed on the task folder's resolved path, so two
checkouts with the same name never share it. State left in a task folder
from before the move is read once and removed where that is allowed.
`CLAUDE_HOOKS_TASKS_STATE_DIR` overrides the root.

- **The files are the record.** Edit them by hand if you like. A hand
  edit wins: the next tool call or session start re-indexes any file
  whose size or mtime changed.
- **The index** is a `tasks` table (plus `task_projects`) on the store
  the host already runs (pgvector or sqlite_vec). It borrows the
  provider's connection, as the mailbox does. It serves cross-project
  listing and recall. Without a SQL store, tasks still work from files
  alone.
- **The project root** is `CLAUDE_PROJECT_DIR` or the current directory,
  walked up to the nearest existing task folder, then to a `.git`
  boundary.
- **Ids carry the project's prefix**, so `bm-42` is unambiguous in mail,
  notes and recall:
  - The prefix is derived once from the project name: the initials of
    its words (`backup_models` → `bm`), or the first two letters of a
    single word (`opencoti` → `op`).
  - On a clash with another project in the index, it is extended
    (`bmo`, `bm2`).
  - It is written to `config.toml` and never re-derived.
  - Set your own with `claude-hooks-tasks init --prefix xx` before the
    first task.

## The task file

```markdown
---
id: bm-42
title: "R9.run3: GEPO brevity with the MoE router in LoRA scope"
status: active            # pending | active | waiting | done | cancelled
priority: H               # H | M | L
area: R9.gepo
tags: [gepo, coderx]
depends: [bm-41]
plan: docs/plans/plan_r9.md
links: {commits: [2f01f52], mail: [614]}
created: 2026-08-26T18:50:00Z
updated: 2026-08-26T19:33:00Z
sessions: [4208dc59]
---

## Description
What and why, kept current.

## Acceptance
- [ ] router LoRA targets present in both ranks
- [x] smoke gates pass

## Log
- 2026-08-26 19:33Z [4208dc59] pending → active: RUNNING on bs2
- 2026-08-26 18:50Z [4208dc59] created
```

- **Description and Acceptance** are rewritten in place.
- **The Log** is append-only, newest first; results go here.
- **Other sections** a person adds are kept.
- **Unknown front-matter keys** survive a round trip.
- **Hand-typed statuses are mapped:** `in_progress` → active,
  `completed` → done, `blocked` → waiting.

## Tools (MCP, on pgvector-mcp and sqlite-vec-mcp)

| tool | does |
|---|---|
| `task-create` | title, description, priority, area, tags, depends, plan, due, acceptance, `start` |
| `task-ready` | active tasks, then open unblocked ones by urgency, plus waiting / blocked |
| `task-list` | filters: status (`open` / `closed` / `all` / one), area (with sub-areas), tag, keywords, since/until, `project="all"`; 20 per page |
| `task-show` | the whole file and its path |
| `task-start` / `task-done` / `task-wait` / `task-cancel` | status change, with an optional log note |
| `task-note` | append a log line |
| `task-update` | change fields; `check` / `uncheck` tick acceptance items by number |
| `task-link` | attach a commit, file, mail, consultancy, plan, url or task |

Writes return immediately; a background thread embeds what changed.

**Urgency** is Taskwarrior's formula, cut down:

| component | weight |
|---|---|
| priority | H 6, M 3.9, L 1.8 |
| active | +4 |
| blocking another task | +8 |
| blocked | −5 |
| waiting | −3 |
| age | up to +2 at one year |
| due date | up to +12, fading over the two weeks before it |

## CLI

```bash
claude-hooks-tasks ready                       # what to work on
claude-hooks-tasks list --status all -q gepo   # search
claude-hooks-tasks list --all-projects         # every project on this store
claude-hooks-tasks show bm-42
claude-hooks-tasks add "title" -p H --area R9 --depends bm-41 --accept "a" "b"
claude-hooks-tasks start|done|wait|cancel bm-42 -m "note"
claude-hooks-tasks note bm-42 "text"
claude-hooks-tasks link bm-42 commit 2f01f52
claude-hooks-tasks init [--prefix xx]
claude-hooks-tasks reindex                     # files → index, then embed what is missing
claude-hooks-tasks board                       # regenerate TASKS.md
claude-hooks-tasks import --transcript <session.jsonl> [--dry-run]
```

`-C <dir>` runs against another project, and `--no-index` uses the files
only. The CLI works from the directory you are in, so its Windows shim
does not `cd` into the repo.

## Hooks

All of these are on by default on every OS. Each has a switch under
`hooks.tasks` in `config/claude-hooks.json`.

| hook | what | key |
|---|---|---|
| SessionStart (startup, resume, `/clear`, compaction) | counts, the active tasks and the top 5 ready ones; a one-line hint in a project with no list | `session_start`, `session_ready` |
| UserPromptSubmit | a summary of every task the prompt names (`bm-42`) | `prompt_mentions` |
| UserPromptSubmit | the tasks most similar to the prompt, open ones first | `prompt_recall`, `recall_k`, `recall_min_score` |
| Stop | block once when the turn edited files while a task is active and no task tool ran | `stop_nudge` |
| PostToolUse | mirror Claude Code's own `TaskCreate` / `TaskUpdate` into the list | `mirror_builtin` |

Notes on the hooks:

- **Prompt recall runs on a thread** next to memory recall, so its
  embedding overlaps recall's instead of adding to it. It skips
  synthetic prompts (task notifications, scheduled ticks).
- **`recall_min_score` is 0.62**, measured on backup_models with
  qwen3-embedding:

  | match | score |
  |---|---|
  | the task a prompt is about | 0.74–0.81 |
  | its close relatives | 0.62–0.65 |
  | same-domain noise | 0.58–0.61 |
  | off-topic prompts | ≤ 0.44 |

  Re-measure after an embedder change.
- **The Stop nudge fires once per change:** once per (active tasks,
  edited files) per session, and never on a continuation. With unread
  mail it joins the mail nudge in one block.
- **The mirror needs the PostToolUse matcher** to include
  `TaskCreate|TaskUpdate`. `install.py` writes it, and `deploy.py`
  reconciles an older one.
- **`hooks.tasks.enabled: false`** turns off the hooks and the MCP
  tools.
- **In a project with a `.claude-hooks-disable` marker** the hooks run
  only if the marker keeps them: `keep: …, tasks`.

## Importing Claude Code's task history

```bash
cd /path/to/project
claude-hooks-tasks import --transcript ~/.claude/projects/<dir>/<session>.jsonl --dry-run
claude-hooks-tasks import --transcript ~/.claude/projects/<dir>/<session>.jsonl
claude-hooks-tasks reindex        # embeddings, in the background if large
```

The importer replays the `TaskCreate` / `TaskUpdate` calls:

- **Ids keep their number** with the prefix (`#856` → `bm-856`).
- **Statuses map:** `completed` → done and `deleted` → cancelled, both
  in `archive/`; `in_progress` → active.
- **Descriptions:** the final one is the Description. Every one it
  replaced is kept under `## Earlier descriptions` with the time it was
  superseded; sessions used descriptions as a running log, so they are
  history.
- **Status and title changes** become Log lines at their original
  times.
- **Dependencies:** `addBlockedBy` / `addBlocks` become `depends`.
- **Re-runs** skip ids that already have a file, so an interrupted
  import resumes.
- **Reading:** a full transcript is read line by line, and only lines
  naming a task tool are parsed.

**Save the input first.** Claude Code deletes a transcript 30 days after
its last use (`cleanupPeriodDays`). To keep the input:

```bash
grep -a -E 'TaskCreate|TaskUpdate|Task #[0-9]+ created' <session>.jsonl \
  > .claude-hooks/tasks-import/<session>-task-calls.jsonl
```

backup_models was imported this way on 2026-10-02:

- 849 tasks: 101 open, 748 archived.
- The extract is kept at `.claude-hooks/tasks-import/`.

## Embedding cost

The embedder runs on CPU at about 80 tok/s, so the size of what is
embedded is the cost:

- **A task's vector** comes from its title, area, the first 500
  characters of the description and the two latest log lines, capped
  at 800 characters. That is about 2.5 s a task; the full description
  measured about 6 s.
- **Vectors are tagged with the provider's table** (`memories_qwen3`).
  A host that changes embedding model re-embeds its tasks instead of
  comparing across spaces.

## Verify

`scripts/verify_deploy.py` checks three things:

- `tasks index`: a real query against the index;
- `PATH wrappers`: `claude-hooks-tasks` and every other `bin/*` CLI is
  on PATH;
- `hook matchers`: the installed blocks match `install.py`.

Tests:

- `tests/test_tasks.py`: format, folder, index on both dialects. The
  Postgres half runs with `CLAUDE_HOOKS_TEST_PG_DSN`, in a throwaway
  schema.
- `tests/test_tasks_tools.py`: tools, CLI, board, importer.
- `tests/test_tasks_hooks.py`: hooks and the marker route.
- `tests/test_deploy_completeness.py`: the deploy coverage.
