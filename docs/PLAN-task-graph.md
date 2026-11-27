# Plan: task graph (ch-28)

Status 2026-11-19: phases 0–4 built on `dev` (774cc39, 6bec7b7, f3f6383,
8391d57, then phase 4); see `docs/tasks.md` for the shipped behaviour.
Differs from this plan: gates are checked at session start and on
request, not by a daemon sweep; the claim field is `claimed_by`.

Long-horizon, cross-project work needs a dependency graph that reaches
across projects and something that tells a session when its work is
unblocked. [beads](https://github.com/gastownhall/beads) (MIT) has the
graph model; this plan adopts that model on top of our storage: Markdown
files per project are the record, and the host index (`tasks` /
`task_projects` on pgvector or sqlite_vec) resolves ids across projects.

Not adopted: Dolt and its sync, hash ids, beads' messaging and
`bd remember` (the mailbox and pgvector cover them), formulas /
molecules / wisps, AI summaries of closed tasks.

## Rules that hold in every phase

1. **A task only ever writes its own project's files.** A link to another
   project's task is stored on our side only; the reverse view (who
   depends on me, my children, who relates to me) is computed from the
   index. No phase writes into another project's tree.
2. **Ids are never renumbered, and never reused.** The prefix is the
   global namespace (`task_projects` keeps prefixes unique); `op-347` means
   opencoti's task 347 everywhere.
3. **An id we cannot resolve is blocking, never done.** Unknown project,
   unknown task, index down: the dependency stays open and says why.
   Today `board()` treats an id it does not know as satisfied
   (`status_of.get(d)` is `None`, not an open status); that changes in
   phase 1.
4. **The files win.** Cross-project answers are as fresh as the other
   project's last reconcile; views say so (`indexed_at`).
5. **Back-compatible.** New front-matter keys are ones older code keeps in
   `Task.extra` and writes back unchanged, so a host that has not deployed
   yet never drops them.

## Phase 0 — one canonical root per project (prerequisite)

Measured 2026-11-19 (read-only):

- The index is clean: one root per project, all 347 `op-` rows from
  `/srv/dev-disk-by-label-opt/dev/opencoti`.
- opencoti has 27 linked git worktrees (`opencoti-rel-*`), claude-hooks 2
  (`claude-hooks-parity`, `claude-hooks-polykv-council`), each with its own
  committed copy of `.claude-hooks/tasks` under the same project and prefix.
  No worktree holds an id its main checkout lacks.

Latent bug: identity is the `project` name, and `reconcile()` registers
whatever root it runs in, upserts that root's files and **deletes every row
whose file that root lacks**. A session in `opencoti-rel-c12` (127 tasks)
would rewrite 127 rows with the branch's older copies, delete 220 live
rows and re-point the project's root. A later reconcile in main heals it
(files are untouched), but cross-project lookups would see opencoti's
open work as gone in between. Separately, `TaskDir.create()` allocates
from the local folder only, so a task created in a worktree can take a
number main already gave out, and the two collide on merge.

Fix:

- **Canonical root.** `files.git_main_root(root)` reads a linked
  worktree's `.git` file (`gitdir:` → `commondir` → the main worktree),
  stdlib only, no `git` subprocess. When the main worktree has a task
  folder with the same project and prefix, it is the canonical root and
  this checkout is a **mirror**.
- **A mirror never writes the index**: no `register_project`, no upsert, no
  delete, no reconcile.
- **Index ownership.** `register_project` records the owner `(host,
  root)`; a different root never overwrites it (another host's clone of the
  same repo included). Ownership moves only when the registered root is on
  this host and no longer holds the task folder (moved or deleted), or by
  `claude-hooks-tasks adopt`. A refused write is logged once per process.
- **Id floor.** `TaskDir.create(floor=)`: the next id is above the local
  folder, the index's highest number for the project, and (for a mirror)
  the canonical folder's highest number.
- **What a session in a mirror does with the task tools** (decided with
  the opencoti integrator, mail #2331 / #2332, matching opencoti's
  `docs/protocols/SESSIONS.md` §5):
  - A worktree on a branch is a **worker**: it may start, note and edit an
    existing task in its own folder (that file change travels on its
    branch and is merged); it never creates, finishes or cancels a task —
    those are refused, naming the main checkout, the single source of ids.
  - A detached worktree (a release snapshot), or one whose directory name
    matches `hooks.tasks.read_only_worktrees` (globs; solidpc sets
    `opencoti-rel-*`, whose checkouts are partly on `release/*` branches),
    is **read-only**: every write is refused.
  - A linked worktree is a mirror even when its main checkout has no task
    folder yet: creating there used to start a new project named after
    the worktree directory.
- **Verification:** before/after snapshots of every `op-` id and file hash
  in the main checkout and of the index rows; a session started in a
  release worktree changes neither.

## Phase 1 — cross-project depends, cycles, typed links

- `depends` takes any `<prefix>-<n>`. A local id must exist (as today). A
  foreign id resolves through the index: unknown prefix is refused (typo
  guard), known prefix with no such task is refused, index unreachable is
  accepted with a warning and stays blocking.
- **Cycle check on every write**, over the union of this project's files
  and the index's open rows across projects.
- `board()` / `task-ready` resolve foreign deps through the index; an
  unresolved dep is blocking with its reason; `blocking` counts include
  dependents in other projects.
- **Typed links** in the existing `links` map, back-compatible:
  `parent` (epic), `discovered-from`, `related`, `caused-by`,
  `supersedes`, `duplicate-of`. Only `depends` blocks. `supersedes` /
  `duplicate-of` close the replaced task when it is in this project; in
  another project they only record the link. `related` is stored on one
  side and shown on both.
- `task-show` gains the reverse view: dependents, children, related, from
  every project.

## Phase 2 — closing a task reports what it unblocked

- `task-done` / `task-cancel` compute, across the index, the open tasks
  whose last open dependency this was, and list them in the reply.
- For each such task in another project, the closing session mails the
  session that owns that project and rings its doorbell (same text shape
  as the mailbox's). `hooks.tasks.unblock_mail` (default on) turns the
  mail off; the reply still lists them.

## Phase 3 — gates

A task waits on something outside the task list through `gates`:

| gate | resolves when | checked |
|---|---|---|
| `mail:<id>` | mail #id is acked (with or without a note) | SessionStart, sweep |
| `human:<what>` | `task-gate resolve` | by hand |
| `timer:<ISO time>` | the time passes | SessionStart, sweep |
| `commit:<sha>@<branch>` | the branch contains the commit | SessionStart, sweep |
| `gh-pr:<repo>#<n>` | the PR is merged | sweep / CLI |
| `gh-run:<repo>#<id>` | the run succeeded | sweep / CLI |

An open gate blocks like an open dependency. A resolved gate is logged and
marked in the task's own file. `gh` gates never run on a hook's hot path.

## Phase 4 — views, claim, defer

- `task-graph` (MCP + CLI): layers (layer 0 can start now; same layer can
  run in parallel), from one id up / down / both or across all projects;
  compact text for the model, `--mermaid` for people.
- `task-blocked`: every blocked task with what blocks it (deps and gates).
- **Claim:** `task-start` records `owner: alias@host`; another live session
  cannot take it without `force` (liveness as resource claims: pid +
  start).
- **Defer:** `defer: <date>` hides a task (and its children) from
  `task-ready` until the date.

## Surfaces and tests

Each phase lands on both memory servers (`tools_for_provider()`), the CLI,
`docs/tasks.md` and `.claude/rules/tasks.md`, with tests in
`tests/test_tasks*.py`; full suite before each commit, Pandorum smoke
before the series is called done.
