# 04 — Sandboxing and shell execution: Cerebriline map, council design, plan

Date: 2026-10-10. Fork: `/shared/dev/cline` @ `400b75535` (main, 1,226 commits
since 2026-08-12). Ours: `/srv/dev-disk-by-label-opt/dev/claude-hooks` @ `52749c5`
(dev). Read-only research: no repo was edited. The only things executed were
read-only host probes and two harmless sandbox runs (section 3), all against a
scratch directory.

Contents

1. Fork map (Cerebriline)
2. Our side map (consultants council)
3. Host capability probes (solidpc, pandorum)
4. What the fork's sandbox does not cover (measured on solidpc)
5. Design: council sandbox
6. Phased plan
7. Risks and open questions
8. Appendix: commit list

---

## 1. Fork map (Cerebriline)

### 1.1 The launcher binary — `sandbox/cerebriline-sandbox/`

Rust, **no external crates** (libc entry points declared by hand), 3,686 lines
across `src/` plus `README.md`. One binary, a per-OS backend chosen at build time.

| file | lines | role |
|---|---|---|
| `src/main.rs` | 232 | CLI parse, backend pick, escape-critical refusal |
| `src/linux.rs` | 429 | L1: user namespace + overlayfs over the workspace path |
| `src/confine.rs` | 275 | write confinement shared by the Linux backends |
| `src/overlay.rs` | 353 | L1 teardown: kernel upper → `.wh.` format |
| `src/linux_l2.rs` | 812 | L2: ptrace path-rewriting supervisor (x86_64 only) |
| `src/resolve.rs` | 292 | L2 path resolution against the overlay |
| `src/macos.rs` | 426 | M1: APFS `clonefile` + Seatbelt confinement |
| `src/windows.rs` | 631 | W1: Detours loader + Low-integrity confinement |
| `../w1-spike/hook.cpp` | — | C++ Detours `hook.dll`: `NtCreateFile` redirect, copy-up, whiteouts, re-injection |
| `tests/{l1,l2,confine,macos,w1}_isolation.rs` | — | per-platform integration tests that skip when the prerequisite is missing |

**CLI contract** (`main.rs:12-21`, `:99-132`):
`cerebriline-sandbox <hook> <log> <command> [args...]` with `CEREBRILINE_WS_ROOT`,
`CEREBRILINE_OVERLAY_ROOT`, optional `CEREBRILINE_SANDBOX_LOG`. `<hook>` is used only
by Windows and kept positional so one `wrapSpawn` shape serves every OS. Other
switches: `CEREBRILINE_SANDBOX_CONFINE=1` (`main.rs:64-68`),
`CEREBRILINE_SANDBOX_DIRECT=1` (confine with no overlay, the lead's mode,
`main.rs:75-79`), `CEREBRILINE_SANDBOX_BACKEND=auto|l1|l2` (`main.rs:174-192`),
`CEREBRILINE_SANDBOX_TMPFS_SIZE` (default `4g`, `linux.rs:58`, `:358-364`).
Exit codes: 64 usage, 70 no backend for the platform, 71 sandbox setup failed,
127 exec failed. On a signal it returns 128+signo (`linux.rs:404-411`).

**The escape-critical rule.** With no working backend the command is refused,
never run unsandboxed (`main.rs:219-231`, `:150-157`). Every confine path fails
closed (`linux.rs:188-192`, `linux.rs:294-298`). The TS side withholds the shell
when no launcher exists (`agent-sandbox.ts:164-190`). We should keep this rule.

#### Linux L1 (`linux.rs`)

- `userns_available()` probes `unshare(CLONE_NEWUSER)` in a throwaway child
  (`:63-78`).
- `run()` creates a sibling scratch directory `<overlay>.ovl-scratch` (`:87`,
  `:102-111`) and refuses paths containing `:` or `,`, which overlayfs options
  cannot express (`:89-100`). It then forks.
- In the child (`:222-338`): `unshare(USER|NS)`; uid/gid map `0 → euid`
  (`:242-249`); `/` made `MS_PRIVATE` (`:252`); a **tmpfs** mounted at the
  scratch directory (`:259`). The tmpfs is there because an unprivileged
  overlayfs upper on disk fails with "failed to set xattr on upper" (header
  `:7-13`). `overlay::preseed` copies the agent's tool edits into the upper
  (`:270`). The overlay `lowerdir=ws,upperdir,workdir` is mounted **over the
  workspace path itself** (`:278-287`), which gives absolute-path fidelity.
- The command runs in a grandchild, optionally under `confine::apply`
  (`:290-311`). Afterwards the child detaches the overlay with
  `umount2(MNT_DETACH)` (`:327-332`) and runs `overlay::reconcile` (`:334`),
  which turns the kernel's `0:0` chardev whiteouts into `.wh.<name>` files
  (`overlay.rs:128-176`).
- `run_direct()` (`:165-216`) is the lead's mode: uid mapped to itself, then
  `confine::apply`, no overlay.

#### Linux confinement (`confine.rs`)

Header `:1-31`: a second mount namespace for the command; `/tmp` and `/var/tmp`
are bound onto themselves so they stay writable (`:73-74`, `:189-202`); every
other mount is remounted read-only with its flags kept (`:204-231`; a failed
remount of `/` or the home mount is fatal, any other is only logged). The
capability bounding set is emptied and `no_new_privs` set (`:233-242`).
**`/proc`, `/sys` and `/dev` are deliberately left alone** (`:25-27`, `:72`).
The header says Landlock was rejected because it is "compiled in and not active"
on the PVE 6.2 kernel (`:29-31`). This is **write** confinement only: nothing
restricts reads, network, PIDs or resources.

#### Linux L2 (`linux_l2.rs`, `resolve.rs`)

A ptrace supervisor in the style of proot (header `:1-21`). It traces the whole
tree (`PTRACE_O_TRACEFORK|VFORK|CLONE|EXEC|EXITKILL`, `:47-52`), rewrites path
arguments of open/stat/access/readlink/mkdir in the tracee's stack scratch, and
emulates unlink/rmdir/rename as `.wh.` operations (`:405-446`). Under
confinement it refuses writes outside the workspace and temp with EACCES
(`:78-102`, `:329-371`), including `mount`/`umount2` (`:340`). Documented gaps:
symlinks are followed lexically, some rarer syscalls are not intercepted, and it
is x86_64 only (README "Scope / gaps"). It exists for AppArmor-locked hosts and
for the **EOVERFLOW** filesystem (README "Known L1 limitation"; this host's ext4
with `jqfmt=vfsv0` quotas). `auto` does not detect EOVERFLOW: you have to force
`l2`.

#### macOS M1 (`macos.rs`)

`clonefile(2)` makes a hidden sibling of the workspace (`:129-158`) and lays the
overlay onto it (`:273-327`). The command runs with its cwd at the clone, and
workspace-rooted argv entries are rewritten (`:247-271`). The change set comes
from diffing the clone against the workspace (`:329-385`). The cost is that
absolute paths built at run time escape (header `:13-21`). Confinement is
`/usr/bin/sandbox-exec` with `(allow default)(deny file-write*)` plus write
allows for the clone, `/private/tmp`, `/private/var/{tmp,folders}` and `/dev`
(`:198-230`). Network is not denied.

#### Windows W1 (`windows.rs`, `w1-spike/hook.cpp`)

The loader starts the command with `DetourCreateProcessWithDllExW`, and
`hook.dll` re-injects itself into every child (header `:1-31`). Confinement
starts the tree at **Low integrity** (`low_integrity_token` `:201`). Every write
is then refused except to objects labelled Low, and the launcher labels the
overlay, the log and a private temp folder (`label_low` `:259`,
`prepare_confinement` `:471`, `run` `:550-578`). There is no Job Object, no
AppContainer and no network restriction. Known gaps: 8.3 names, specific-wildcard
directory queries fall through unmerged (`cmd del` is unreliable), and
command-line quoting is not escaped (README "Known W1 gaps").

#### Build, CI and shipping

- `.github/workflows/sandbox-binaries.yml` builds six artifacts on native
  runners and commits them to `apps/vscode/assets/sandbox/`. Linux runs on
  `ubuntu-latest` with `./build.sh` (x64 + arm64 cross), macOS on `macos-14`,
  Windows on `windows-latest` (Detours + `hook.dll` + `cargo build`).
  `sandbox-test.yml` runs the isolation tests on all three.
- `apps/vscode/assets/sandbox/README.md`: "Do not edit these by hand". A release
  never rebuilds; `.vscodeignore` re-includes the folder.
- Resolution: `sdk/.../runtime/sandbox/sandbox-binaries.ts:17-76` picks
  `cerebriline-sandbox.exe` + `hook.dll` on win32 and an arch-suffixed name on
  linux/darwin, and `chmod 0755`s it because a vsix zip drops the bit (`:65-72`).

### 1.2 TypeScript half — overlay, wrapSpawn, delegated sandboxes

- **`overlay-fs.ts`** (325 lines). `AgentOverlay` is an in-process
  copy-on-write view used by the agent's file tools. `.wh.` whiteouts
  (`:21`); `resolveRead/resolveWrite/unlink/rename/readdir/changedFiles`
  (`:148-325`); `changedFiles()` skips copy-ups whose content equals the lower
  (`sameContent` `:57`). It shares one on-disk format with the native launcher.
- **`agent-sandbox.ts`** (296 lines).
  - `wrapSpawn` (`:168-190`) roots the shell at the launcher with cwd = the
    workspace and puts the workspace **last** on PATH (`withWorkspaceOnPath`
    `:106-129`). It also injects the `CEREBRILINE_*` env plus `confinementEnv`
    (`:85-95`; Windows gets a private TEMP).
  - The log and the temp directory are siblings of the overlay root, never
    inside it (`:153-162`).
  - `dispose()` removes the overlay and temp and gzips the log (`:197-204`,
    `archiveSandboxLog` `:217-241`).
  - `createLeadConfinement` (`:255-296`) is direct mode for the lead's own
    commands. It throws when it cannot confine (`:270-275`).
- **`delegated-sandboxes.ts`** (454 lines). One registry for spawn_agent,
  swarm, configured agents and teammates (rules header `:14-24`):
  - `run_commands` is offered only when there is a launcher **and** the user's
    "Agents can run commands" toggle is on.
  - Hand-back folds changed files into the lead's revision log as
    **held revisions**, "never written to disk" (`:161-212`).
  - Disposal hands back first and is idempotent (`:239`).
  - `handbackNote` (`:409-451`) tells the lead: "your own files are UNCHANGED…
    To apply it: `restore_file` with the same `revision`".
- **`local-runtime-host.ts:852-890`**. Overlay isolation is always on for
  delegates. The shell is gated by `subagentCommandsEnabled` (`:864`).
  Overlays live under the **persistent session directory**
  `sessionDir/agent-overlays/<toolCallId>`, "never tmpfs" (`:870-871`, `:882`).
  `subagentSandboxConfine === false` turns confinement off (`:878-880`).
  Default confine is on (`agent-sandbox.ts:152`, commit `e9f240477`).
- **`apps/cli/src/utils/sandbox-binaries.ts`** (38 lines) is the CLI's
  resolver.

### 1.3 Revisions and the held-revision review/apply flow

- `runtime/atomic/file-revisions.ts` (889 lines) is a per-file, append-only
  history numbered `#1..#N`. `#1` is the transaction base (header `:1-32`).
  `FileRevision.held` means "kept for reading and restoring, never written to
  this workspace" (`:62-63`). `dropped` releases the content under the memory
  caps: 32 MiB per file, 128 MiB in total (`:107`, `:118`). Default list limit
  10 (`:131`).
- `restore-file-tool.ts` (656 lines) puts one file back to any revision. It is
  bounded and self-counting (header `:1-29`). Applying an agent's held revision
  is handled at `:647`. **Apply = `restore_file revision:"#N"`; review =
  `read_files revision:"#N"`**. Nothing is auto-applied (commit `57fc361b0`
  message).
- Commits: `2c8997b25` (a file has a history), `9d6dd4ea6` (revisions the model
  can choose between), `4a0db2d22`/`f331d6af2`/`2459f758e` (caps and retention
  across compactions), `791e9c9a5` (the lead reaches its agents' work with
  Checkpoints off), `ea01be246` (held revisions are not the file).

### 1.4 Snapshots for non-git directories

There are two independent mechanisms.

**(a) Git checkpoints** (`hooks/checkpoint-hooks.ts`, 481 lines). A
stash-compatible snapshot commit that, unlike `git stash create`, also captures
untracked files as a third parent (`:110-217`). It is kept reachable under the
private ref `refs/cline/checkpoints/<sessionId>/<runCount>` (`:397-403`) and GC'd
by `deleteCheckpointRefs` (`:250-274`) / `retainCheckpointRefs` (`:276+`).
Restore (`session/checkpoint-restore.ts`) **throws on a non-git directory**
(`:55-59`, `:382-386`).

**(b) The non-git snapshot** (`runtime/atomic/snapshot.ts`, 219 lines). The
header `:9-20` explains it: Cline's checkpoints throw on a plain directory, and
a git 2.30 `add --sparse` failure once silently wrote an empty tree. So this is
"a copy with a checksum manifest and no git in it anywhere".

- **Taken:** `takeSnapshot(root)` (`:63-117`) walks the tree, skips
  `SCAN_IGNORED_DIRECTORIES` (`oracle.ts:145`: `.git`, `node_modules`, `dist`,
  `build`, `out`, `target`, …) and **records symlinks as skipped**. It reads
  every file into **RAM** with a sha256, capped at 5,000 files, 4 MiB per file
  and 128 MiB in total (`:45-49`). Files over the caps go to `skipped` and are
  never rolled back.
- **Stored:** only in process memory, inside the transaction object
  (`transaction-controller.ts:335`, `:488`; `escalation-session.ts:374-417`).
  It is never written to disk.
- **Diffed:** `snapshotChanges` (`:190`) and `snapshotIsClean` (`:205`)
  re-walk and compare hashes.
- **Restored:** `restoreSnapshot` (`:139-175`) rewrites changed files,
  recreates deleted ones, removes created ones and reports `uncovered`.
- **GC:** the snapshot is dropped with the transaction. Delegated overlays are
  removed on `dispose()`, and deleting a history item purges overlays and
  snapshots (`3c4c9564a`, history-ux #93). **Not found:** any startup sweep of
  orphaned `agent-overlays/` after a host crash. The only reaper-shaped code is
  the detached-command log reaper (`executors/bash.ts:263`) and the swarm's
  "60-second ephemeral sweep" mentioned in `spawn-swarm-tool.ts:23`.

Fitness for a huge tree: **(b) does not scale**. On a 136 GiB `scripts/`
subtree it would hit the 128 MiB cap on the first few files and leave the rest
"uncovered". (a) needs git. Neither is reflink- or overlay-backed. The **L1/M1
overlay or clone** is the fork's cheap copy-on-write path, but it covers only
the delegated agents' shell, not snapshots.

### 1.5 Terminal and shell tool

- `extensions/tools/executors/bash.ts` (1,190 lines). `ShellExecutorOptions`
  (`:397-477`) covers `timeoutMs`, `maxOutputChars`, `env`, `wrapSpawn`
  (`:459`) and `detachedLogRetentionMs`.
  - The child env inherits `process.env` merged with config (`:487-509`): no
    scrubbing.
  - Spawn is `detached:true` off Windows (`:796`). A timeout kills the process
    group with `SIGKILL`, or `taskkill /T` on Windows (`:830-896`).
  - A command still running at the deadline is detached to a log file
    (`:904-941`). The logs are reaped after a retention window (`:263`).
- The VS Code terminal-backed variant is
  `apps/vscode/src/hosts/vscode/terminal/VscodeTerminalProcess.ts`. Commits
  `fc6929a2b` (end a command whose end VS Code never reports), `acfda70f7`
  (an unfinished command at `>>` is reported, not waited on), `431eb4522`
  (abandoned terminals close), `3c6554791`, `233c60db4`.
- **`command-guard.ts` (521 lines)**, a plan-mode blacklist. The header
  (`:1-18`) says it plainly: "a simple blacklist, not a shell interpreter… will
  not catch every possible mutation". `BLOCKED_COMMANDS` starts at `:23`.
  `command-guard-extension.ts` (143 lines) enforces it as a `beforeTool` hook
  before approval. `sed in_place` is refused separately (`:45-60`).
- `911d435b3` "ask the filesystem what a command changed, not the command line"
  stat-sweeps the files the session has read, before and after each command
  (bounded at 512). This is the right model: **observe effects instead of
  parsing commands**.
- `3c433d5a9`: the run_commands object form without args routes through the
  shell. `d9bb22883`: PowerShell fail-fast semantics. `b136194ce`:
  `JSON.stringify` is not Windows shell quoting.

### 1.6 Tool permission, approval and read-only classification

- `shared/src/llms/tools.ts:7-18`: `ToolPolicy {enabled?, autoApprove?}`, both
  defaulting to true. That is the whole policy vocabulary: two booleans, no
  ladder.
- Approvals are rendered in the chat (`0a9209458`). Auto-approve domains:
  `099a6e242` (grep/sed/awk were in no domain). MCP auto-approve: `2b7b01328`,
  `f267ccc92`, `833cc891b`. CLI batches self-approve checks (`442d9cabe`).
- **Read-only classification.** `shared/src/agent.ts:270-284` `readOnly?:
  boolean`, where "Unset means it may write". MCP tools take it from
  `annotations.readOnlyHint` (`extensions/mcp/client.ts:358-380`,
  `mcp/types.ts:7-8`, `mcp/tools.ts:55`). Built-ins mark it explicitly
  (`list-files.ts:454`, `definitions.ts:439`, `ask-lsp.ts:316`, …). The xOllama
  vendor maps it to `x_read_only: true` on the wire, so a council offers
  researchers and critics only read-only tools and only the synthesizer writes
  (`llms/.../xollama.ts:14-17`, `markReadOnly` `:444-475`, commit `9f20d6937`).
  `run_commands` is unmarked, which means write.

### 1.7 Network handling

**Not found in any backend.** A grep of `sandbox/` and
`sdk/packages/core/src/runtime` for `NEWNET|NEWPID|seccomp|landlock|cgroup|rlimit|JobObject`
returns only prose mentioning seccomp in `linux_l2.rs:7` and the README. The
Seatbelt profile does not deny `network*`, and Low integrity does not block
sockets. A sandboxed command has the host's full network.

---

## 2. Our side map (consultants council)

### 2.1 The plan — `docs/PLAN-council-tool-surface.md`

- Status header `:3-12`: M-A partially landed and M-C landed. The header still
  says the lane-scoped interrupt is missing. That is **stale**:
  `consultants/engine/tool_approval.py` now provides the channel (2.4).
- Ladder `:133-154`: `auto / ask_assistant / ask_human / deny`. `auto` must stay
  a dict lookup.
- **M-D, shell provider, `:218-240`**: a pattern allowlist; "read-only is not
  decidable from a command string" (`:223-228`); fail closed, with unmatched
  commands going to `ask_assistant` (`:234-235`); every escalation must carry
  the command, the root, the why and the class (`:237-239`).
- **Sandbox parity `:367-369`**: "`make_executor(extra_roots)` confines path
  tools today; shell and git must honor the same roots or the sandbox has a
  side door."
- Taint invariant `:373-455`: sticky, one rung up, inherited by follow-ups and
  persisted with the consultancy (`:450-455`).
- Estimate `:492`: M-D is L, about 900 LOC, "dominated by the classification
  table". **This design moves the safety from the classification table to the
  sandbox**. The table stays, but only as a cost and egress classifier
  (section 5.6).

### 2.2 Registry, gate and providers — `claude_hooks/tool_registry/`

- `base.py:33-106`: `ToolProvider` with `specs / execute / default_level /
  is_read_only / taints / owns / close`. Errors are returned as strings, not
  raised (`:53-62`). `is_read_only` "is a safety assertion… When unsure, say
  False" (`:69-77`).
- `policy.py`: ladder (`:66-92`); `PolicyGate.resolve` order is runtime
  control → `[tools.permissions]` → provider default → `default_level`
  (`:148-161`, `:216-226`); an invalid level fails closed to `deny`
  (`:180-193`); taint escalates non-read-only tools (`:195-208`).
- `registry.py`: one dispatch path (`:123-156`); with no approval channel every
  `ask_*` is refused (`:159-178`); `as_executor(runtime_permissions=…)`
  (`:181-191`).
- `git_provider.py`: fixed argv, never a shell string, and an allowlist of
  `log/blame/diff/show/rev-parse` (`:41-43`). 20k-char output cap (`:48`),
  30 s timeout (`:52`), `_resolve_in_roots` (`:59-73`). It is the model for how
  a shell provider should bound its output.

### 2.3 Path sandbox — `claude_hooks/caliber_proxy/tools.py`

- `resolve_in_roots` (`:36-76` and on): realpath-then-check against
  `[cwd, *extra_roots]`, so symlinks out of every root are rejected. Relative
  paths fall back across extra roots.
- `TOOL_IMPLS` (`:546-553`), `_PATH_AWARE_TOOLS` (`:560`) and `execute`
  (`:563-591`) never raise. `make_executor(extra_roots)` (`:594-613`).

### 2.4 Approval channel — `consultants/engine/tool_approval.py` (583 lines)

- `ask_assistant` auto-approves and records an event (header `:9-17`).
- `ask_human` parks the lane until `DEFAULT_APPROVAL_TIMEOUT_S = 600` (`:84`)
  and **a timeout denies** (`:26-30`).
- Requests are coalesced per council, and standing `GrantRule`s cover a tool or
  a glob (`:32-52`, `:181-246`).
- Per-lane parking comes from blocking inside the executor thread (`:54-60`).
- Runner wiring: `_make_tool_approval_fn` (`runner.py:1740-1785`).
- CLI: `tool-ack <sid> --allow|--deny [--all-of-tool|--all-matching]`
  (`SKILL.md:1170-1191`).

### 2.5 Tool surface, cache and lane identity

- `consultants/server/tool_surface.py:59-61`: the provider list is where a
  `ShellToolProvider` goes. `:96-101` builds the registry.
- **Finding A.** `tool_surface.py:100` calls `registry.as_executor()` **without
  `runtime_permissions`**, so `runtime_control.tool_permissions` (settable via
  `POST /control`) never reaches the gate. A running council cannot be
  tightened from the control surface. Today that is harmless, because every
  tool is `auto`. Once shell exists it is a missing kill switch.
- **Finding B.** `consultants/server/control.py:55`
  `VALID_TOOL_PERMISSION_VALUES = ("allow","deny","ask")` validates the old
  3-value enum, while the gate understands `auto/ask_assistant/ask_human/deny`
  (`policy.py:66-72`). If A were fixed as-is, an operator's `"allow"` would be
  treated as an invalid level and **denied** (`policy.py:180-193`), and
  `"ask"` likewise. The plan flagged the enum change (`:135-137`). It has not
  been done.
- `consultants/server/tool_cache.py`: the cache is per run and shared across
  lanes. `CACHEABLE_TOOLS` (`:37`). **Any non-read-only call clears the whole
  cache** (`:19`, `:95-106`). As specified, a shell call (`is_read_only=False`)
  would therefore flush every lane's cache on every command. In this design a
  sandboxed shell **cannot change the real tree**, so it must not invalidate the
  real-tree cache (section 5.7).
- **Lane identity is not visible to the executor.** The signature is
  `(name, args, cwd) -> str`, and one shared executor serves every lane
  (`graph.py:415-430`: `tool_executor=deps.tool_executor`). The Send state does
  carry `lane_idx` / `parent_lane_idx` (`graph.py:119-127`), so the node
  wrapper can bind a per-lane executor.

### 2.6 Coder sandbox — `consultants/engine/coder.py`

- Writes only under `<cwd>/.claude-hooks/consultants/<sid>/coder-out/`
  (`sandbox_root_for` `:240-246`).
- Path normalisation rejects absolute paths and `..` (`:209-237`). There is a
  post-resolve escape check (`:331-340`) and an atomic temp+fsync+replace
  (`:341-354`).
- Caps: 50 KB per file, 1 MB per session, 16 files (`:358-371`, checks
  `:296-327`).
- **Not found:** any GC of `coder-out/`. Session directories under
  `.claude-hooks/consultants/<sid>/` are never reaped. The idle reaper
  (`app.py:2131-2160`) only evicts in-memory sessions.

### 2.7 Follow-ups (the runtime-addable path)

- Route `app.py:860-1045`:
  - The body accepts `effort`, `allow_extra` (`:880-887`), `trace` (`:898`),
    `extra_roots` (`:917-924`, merged `:933`) and `skip_preflight`.
  - The child `SessionState` persists `extra_roots` (`:956-976`).
  - `runner_input` (`:1008-1023`) and the `inflight.register` record
    (`:1024-1036`) carry the same fields, so resume after an engine restart
    rebuilds the identical run.
- **`run_follow_up` rebuilds the tool surface from scratch per follow-up**
  (`runner.py:825-829`, `build_tool_surface(cfg, extra_roots=…,
  approval_fn=…)`). That is the natural hook: a follow-up can carry a
  shell-enable override and get a new surface without touching the parent's
  run.
- The follow-up graph contains only researcher, critic (high/max),
  synthesizer and adversary (`graph.py:1652+`, add_node at `+7..+15`). The
  coder and tool_executor are not in it. **So in a follow-up, shell means
  researcher lanes with a `run_command` tool.**
- CLI: `cmd_follow_up` (`cli.py:283-330`) builds the body (`:293-307`).
  `--add-dir` (`:1899`), `--allow-extra` (`:1920`), `config set-tools`
  (`:889`, `:2369-2414`).
- Skill: follow-up invocations at `SKILL.md:197`, `:436`, `:521`, `:544`;
  tools config dialog `:1066-1134`; approvals `:1146-1198`.
- Engine process: `~/.config/systemd/user/claude-hooks-consultants.service`
  runs `python -m consultants.server` **as root, with no systemd sandboxing
  directives**. Whatever the shell runs inherits root unless the launcher drops
  it.

---

## 3. Host capability probes

### 3.1 solidpc (Linux)

| probe | result | consequence |
|---|---|---|
| `uname -r` | `6.2.16-20-bpo11-pve` (Debian bullseye) | has userns, `cgroup.kill` (5.14+) and seccomp user-notif; Landlock ABI ≤ 2, no Landlock network rules (6.7+) |
| `unprivileged_userns_clone` / `max_user_namespaces` | `1` / `512415` | user namespaces available |
| `/sys/kernel/security/lsm` | `capability,yama` | **Landlock not active.** `CONFIG_SECURITY_LANDLOCK=y`, `CONFIG_LSM="yama,integrity,apparmor"`; `landlock_create_ruleset(VERSION)` returns `EOPNOTSUPP`. Enabling it needs a boot parameter `lsm=landlock,yama,integrity,apparmor`. AppArmor is not active either. |
| seccomp | `CONFIG_SECCOMP_FILTER=y`; actions include `user_notif` | seccomp-bpf is usable |
| `CONFIG_BPF_LSM` | `y` (not in the active list) | not usable without a boot parameter |
| cgroups | cgroup2 unified at `/sys/fs/cgroup`; the engine is in `user.slice/...` | `memory.max`, `pids.max`, `cpu.max`, `cgroup.kill` |
| namespaces | `PID_NS NET_NS IPC_NS UTS_NS TIME_NS` = y | full namespace set |
| overlayfs | module, **`CONFIG_OVERLAY_FS_XINO_AUTO=y`**, `CONFIG_TMPFS_INODE64=y` | see EOVERFLOW note below |
| FUSE | `CONFIG_FUSE_FS=y`, `fusermount3` present; `fuse-overlayfs` **absent** | — |
| `bwrap` | `/usr/bin/bwrap` 0.4.1; `unshare`, `systemd-run` present | **tested:** `--unshare-all --unshare-user --uid 65534` gives `uid=nobody`, `/proc/net/dev` shows only `lo`, 4 PIDs visible, and a write to the repo returns `Read-only file system` |
| yama `ptrace_scope` | `1` | — |
| engine identity | root (uid 0) | the sandbox must drop to an unprivileged uid; it must not just drop caps |
| `/srv/dev-disk-by-label-opt` (repo, `/shared/dev`) | ext4 on `nvme4n1p3`, `jqfmt=vfsv0,usrjquota,grpjquota`, old features (`uninit_bg`, 256-byte inodes, no `metadata_csum`); 165 G free | **the fork's EOVERFLOW host.** No reflink. |
| `backup_models` | **XFS on `/dev/bcache0`, `reflink=1`**, `inode64`, `usrquota,grpquota` (no `prjquota`); 50 T, 19 T free | reflink copy-on-write is available. Project quotas are not mounted. |
| `backup_models` shape | no `.git`; 212 top-level entries, 4,913 entries at depth ≤ 2; `scripts/` = **136 GiB**; already has `.claude-hooks/{consultants,tasks,…}` | copying is not an option, and walking is expensive |
| `/` | ext4, **96% full (29 G free)** | never put sandboxes under `~/.claude` or `/var` on this host |
| `/tmp` | tmpfs 63 G, RAM-backed | never put view uppers or snapshots there (CLAUDE.md rule). A *private, size-capped* tmpfs inside the sandbox mount namespace is a different thing and is fine. |
| RAM / CPU | 125 G / 12 | — |
| docker | `docker0` and many bridges present | a container backend is possible but heavy and Linux-only (rejected, section 5.2) |

**EOVERFLOW: a hypothesis, unverified.** The fork attributes copy-up EOVERFLOW
to "ext4 with quotas on bcache". On this kernel two config facts give a more
specific mechanism:

- `CONFIG_OVERLAY_FS_XINO_AUTO=y` means overlayfs folds the layer fsid into the
  high inode bits whenever the layers are on different filesystems. The fork's
  layout always is: lower on disk, upper on tmpfs.
- `CONFIG_TMPFS_INODE64=y` means the fork's self-mounted tmpfs upper issues
  64-bit inode numbers.

A copied-up file whose upper inode number does not fit in `64 − xino_bits` would
then be reported as EOVERFLOW (dmesg "inode number too big"). The test is to
mount with `xino=off`, or mount the tmpfs with `inode32`, and repeat a copy-up
on the repo's ext4. The P0 probe in section 6 does exactly that. If confirmed,
a one-option fix restores overlayfs on this host, and the L2 ptrace fallback is
unnecessary for it.

**Measured 2026-10-14 (`scripts/sandbox_probe.py --overlay`, Milestone E1):
the hypothesis is refuted.**

- As real root, every overlay variant works.
- Inside a user namespace, with the lower layer on the host filesystem (ext4 here):
  - an upper on tmpfs fails copy-up with EOVERFLOW under `xino=auto` and `xino=off` alike, with or without `inode32`;
  - an upper on the host filesystem refuses to mount ("cannot mount overlay read-only").
- With both lower and upper on tmpfs inside the namespace, it works.
- So the trigger is a host-filesystem lower inside a user namespace, not `XINO_AUTO`/`TMPFS_INODE64` and not quotas.
- Consequence for E3: an unprivileged overlay over a repo is not a usable lane view on this kernel. Lane views use:
  - a git worktree;
  - a reflink clone (XFS `backup_models`: works; ext4 repo: no reflink);
  - a capped copy;
  - or an overlay mounted by a privileged helper before the namespace drops capabilities.

### 3.2 pandorum (Windows)

`ver`: Windows 10 **19045** (22H2). C: is **NTFS**. Dev Drive / ReFS block
cloning (Win11) is therefore not available, and a non-git copy-on-write view
needs the Detours overlay (the fork's `hook.dll`) or a capped copy. AppContainer
and Job Objects are available on Win10.

---

## 4. What the fork's sandbox does not cover (measured on solidpc)

The CI-built `apps/vscode/assets/sandbox/cerebriline-sandbox-x64` **does not
run on solidpc**: `GLIBC_2.32/2.33/2.34 not found`. Bullseye has glibc 2.31,
and the binary was built on `ubuntu-latest`. In the shipped vsix the shell is
therefore withheld on this host. That fails closed, but the shell is lost. The
same applies to any Debian-11-era host. The lesson: **build static musl**.

With the locally built `target/release/cerebriline-sandbox`, a direct-mode
confined run on solidpc (`CEREBRILINE_SANDBOX_DIRECT=1`, workspace = a scratch
directory) gave:

| check inside the confined command | result |
|---|---|
| `id` | `uid=0(root)`: **real root**, because uid is mapped to itself (`linux.rs:179`) and the engine is root |
| `head -c 512 /dev/nvme4n1` | **512 bytes read from the raw system disk** |
| `test -w /dev/nvme4n1` (`access(2)`, DAC only; no write attempted) | **passes**. `/dev` is left untouched (`confine.rs:25-27`), and root owns the node, so no capability is needed. |
| read `~/.claude.json`, list `~/.ssh` | **readable**: no read confinement |
| write `/tmp/x` | **succeeds**: host `/tmp` is shared and writable (`confine.rs:73`) |
| write `/root/x` | refused (`Read-only file system`) — the one thing it is built to stop |
| `/proc/net/dev` | **84 host interfaces**: full network |
| `/proc` PIDs | **3,350 host processes visible**: no PID namespace, so signalling same-uid processes is possible |
| `Seccomp:` | `0` |

The fork's model is "keep the lead's files clean". It is not built to stop a
hostile command, and on a root host it is a thin layer. The fork also has no
resource limits (fork bomb, memory, disk outside the 4g tmpfs, CPU). Time is
bounded only by `bash.ts` timeouts.

---

## 5. Design — the council sandbox

### 5.1 Goals and non-goals

Goals:

1. A shell for council lanes whose **blast radius is the lane's private view**:
   - no writes to the real tree, no writes to the host;
   - no reads of host secrets;
   - no network by default;
   - no visibility of host processes;
   - bounded CPU, memory, PIDs, disk and time.
2. **Absolute-path fidelity**: the repo appears at its real path.
3. Cheap views on huge non-git trees.
4. Lifecycle without stale worktrees or disk exhaustion, crash-safe.
5. Off by default; enabled per request; a user "always" knob; addable in a
   follow-up.
6. Windows and macOS parity as close as each OS allows, stated honestly where
   it falls short.

Non-goals: defending against kernel 0-days (that needs a VM, offered as a later
backend in section 7), and mid-invoke enabling of shell in a running council
(`feedback_langgraph_midinvoke_state_unreadable`). Use a follow-up instead.

### 5.2 Architecture

```
lane node (researcher/tool_executor)                engine (root, unconfined)
  │ run_command{argv|cmd, cwd, timeout}               │
  ▼                                                    │
ToolRegistry.dispatch ─► PolicyGate (ladder+taint) ─► ShellToolProvider
                                                       │  bound to (sid, lane_idx)
                                                       ▼
                                                LaneViewManager ── store: <root>/.claude-hooks/consultants/<sid>/lanes/<lane>/
                                                       │   view = git-worktree | overlay | reflink | readonly+scratch | copy
                                                       ▼
                                   council-sandbox run --spec <fd 3, JSON>   (deploy artifact, static)
                                     Linux:  cgroup leaf → userns(→65534) → mountns(pivot_root) → pid/net/ipc/uts ns
                                             → rlimits → caps drop + no_new_privs → seccomp → [landlock if active] → exec
                                     macOS:  Seatbelt profile (deny network*, scoped writes, secret-path read denies) + rlimits + pgid
                                     Win:    AppContainer (no caps) + Job Object (kill-on-close, mem, procs, cpu) [+ Low IL fallback]
                                     → JSON result on fd 4 {exit, signal, killed_by, wall_ms, cpu_ms, peak_rss, stdout/err (capped)}
```

**The launcher owns isolation; Python owns policy and lifecycle.** This is the
fork's split (`wrapSpawn` vs `cerebriline-sandbox`), with a structured spec on
an fd in place of env vars. Env-var specs leak into the child, and the fork has
to filter them out.

Backends considered and rejected as the primary:

- **bwrap**: present (0.4.1) and verified working. It lacks cgroups, and its
  `--disable-userns` is 0.8+. It is kept as a **Linux fallback backend**
  (`backend = "bwrap"`, wrapped in a `systemd-run --scope` for limits) so P1
  can ship before the Rust launcher is hardened.
- **docker/podman**: heavy and Linux-only, and needs a daemon or rootless setup.
- **The fork's L2 ptrace**: TOCTOU-prone path rewriting, x86_64 only. We do
  not need path rewriting (5.3).
- **gVisor/Firecracker VM**: strongest, but heavy. It is the possible "P6
  paranoid" backend.

### 5.3 Linux backend: layers in setup order

All mounts happen **inside the command's own mount namespace**. A crash
therefore cannot leave a host mount behind. This is the most important
crash-safety property, and it is why the view is not mounted in the host
namespace.

1. **cgroup v2 leaf.** `systemd-run --user --scope --slice=claude-council.slice
   -p MemoryMax= -p MemorySwapMax=0 -p TasksMax= -p CPUQuota= [-p IOWeight=]`,
   or a direct cgroupfs leaf when the launcher runs as root under a delegated
   subtree.
   - Defaults per command: memory 4 GiB, pids 256, CPU 400% (4 of 12 cores),
     and the per-lane wall clock from the tool call (cap 600 s).
   - Teardown writes `cgroup.kill` (kernel ≥ 5.14). The private tmpfs pages
     are charged to this memcg, so `/tmp` inside the sandbox cannot exhaust
     host RAM.
2. **User namespace**, with `setgroups deny`. As root, map inner 0 to host
   **65534** (nobody), or to a dedicated `claude-council` uid when one exists.
   Unprivileged engines map to self (the fork's mode).
   - Effect: an escape lands as nobody, not root.
   - It fixes the raw-disk read in section 4: DAC denies `/dev/nvme*` to nobody.
   - The view directories are chowned to that uid when they are created.
3. **Mount namespace with a fresh root.** tmpfs as `/`, then `pivot_root` and
   detach the old root. Binds:
   - `ro`: `/usr`, `/bin`, `/sbin`, `/lib*`, `/etc` (or a filtered `/etc`:
     `passwd`, `group`, `hosts`, `resolv.conf`, `ssl`, `alternatives`,
     `ld.so*`), and configured toolchain roots (`/root/anaconda3/envs/<env>`,
     `/opt/llama.cpp`, …) declared under `[tools.shell.ro_binds]`.
   - **`rw`: the lane view, bound at the original repo path.** This gives
     absolute-path fidelity without overlayfs (5.4).
   - `ro`: the other council `extra_roots` at their real paths. This is plan
     `:367` sandbox parity: the shell sees exactly the roots the path tools see.
   - Private `/tmp` tmpfs (size 1 GiB, `inode32`).
   - `/dev` minimal: `null zero full random urandom tty`, plus a new `devpts`
     instance with `ptmx`.
   - Fresh `/proc` (`hidepid=2` not needed because of the PID namespace).
   - **No `/sys`**, or a read-only `/sys/devices/system/cpu` only.
   - `HOME=/home/sandbox` (tmpfs). There is **no `~/.ssh`, `~/.claude*`,
     `~/.config/gh`, `.netrc`, conda credentials or Ollama keys**. Read
     confinement is the biggest gain over the fork.
4. **PID, IPC, UTS and network namespaces.**
   - PID: the command is PID 1's child, with a tiny init in the launcher that
     reaps zombies and forwards signals.
   - `PR_SET_PDEATHSIG(SIGKILL)` ties the tree to the launcher, and the
     launcher to the engine.
   - `setsid` plus a new pty blocks TIOCSTI injection.
   - The **network namespace has only `lo`** by default (verified with bwrap
     in 3.1).
5. **rlimits.** `CORE=0`, `NOFILE=4096`, `FSIZE=1 GiB`, `NPROC` (backup to
   `pids.max`), optional `AS`.
6. **Credentials.** Empty the capability bounding set, clear ambient caps,
   `PR_SET_NO_NEW_PRIVS`, `PR_SET_DUMPABLE=0` for the launcher.
7. **seccomp-bpf.** Default allow, with this denylist returning `EPERM`, plus
   `KILL` for a non-native arch or x32:
   - mount family: `mount`, `umount2`, `pivot_root`, `move_mount`, `fsopen`,
     `fsconfig`, `fsmount`, `fspick`, `open_tree`, `mount_setattr`;
   - namespaces: `unshare`, `setns`, and `clone` with any `CLONE_NEW*` flag
     (`clone3` returns `ENOSYS` so libc falls back to `clone`);
   - tracing and memory access: `ptrace`, `process_vm_readv/writev`,
     `userfaultfd`, `perf_event_open`, `bpf`;
   - kernel and modules: `kexec_load`, `kexec_file_load`, `init_module`,
     `finit_module`, `delete_module`;
   - keys: `add_key`, `request_key`, `keyctl`;
   - `io_uring_setup/enter/register`, whose ring operations bypass syscall
     filtering;
   - handles: `open_by_handle_at`, `name_to_handle_at`;
   - system: `swapon`, `swapoff`, `reboot`, `acct`, `quotactl`, `iopl`,
     `ioperm`, `syslog`, `settimeofday`/`clock_settime`;
   - `ioctl(TIOCSTI|TIOCLINUX)`;
   - `socket(AF_PACKET|AF_NETLINK except NETLINK_ROUTE)`.

   Generated in the launcher as raw BPF with no crates, in the fork's style.
8. **Landlock, opportunistic.** Probe the ABI, and when active add filesystem
   rules mirroring the binds. That is defence in depth against a bind-setup
   bug. On 6.2 the filesystem ABI is ≤ 2, and network rules need ABI 4 (6.7+).
   On solidpc today the probe reports `compiled-in, inactive (needs lsm= boot
   parameter)`. The probe output names the exact parameter, and verify_deploy
   shows it as INFO, never FAIL.
9. **Env allowlist.** `PATH` (ro-bind toolchains first, view last, as the fork
   does in `withWorkspaceOnPath`), `LANG/LC_*`, `TERM`, `HOME`, `TMPDIR`,
   `PYTHON*` from config. Everything else is dropped: `*_TOKEN`, `*_KEY`,
   `ANTHROPIC_*`, `OLLAMA_*`, `AWS_*`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`, and so on.
10. **exec.** The command is an argv array when the model supplies one, or
    `/bin/sh -c <string>`. The string is still parsed for classification
    (5.6), never for safety.

**Escape-critical rule (kept from the fork).** If a layer configured as
`required` cannot be set up, the launcher exits with 71 and runs nothing. The
default required set on Linux is user, mount, PID and network namespaces, the
cgroup, no_new_privs and seccomp. Landlock is `optional`. A host that cannot
meet the required set gets no shell, and the reason is shown in `config show`
and preflight.

**Network modes** (`[tools.shell] net`):

- `none` (default): `lo` only.
- `proxy`: a host-side filtering HTTP CONNECT proxy, owned by the M-F network
  provider and enforcing the same allow/deny domain lists. It is reached
  through a unix socket bind-mounted into the sandbox, plus a 30-line in-ns
  TCP→unix forwarder on `127.0.0.1:3128` with `HTTP(S)_PROXY` set. Any `proxy`
  use **taints on first response** (plan `:398`).
- `host`: the host network namespace, rung `ask_human` always.

### 5.4 Lane views ("snapshots") and the store

One `LaneView` per (sid, lane_idx) that the shell touches, created lazily on the
first `run_command`. A lane that never runs a command costs nothing. Each view
has `base` (an immutable reference), `merged` (what the command sees at the real
path), `changes()`, `apply(paths?)`, `discard()` and a `state`. Kinds, chosen by
`probe` plus the per-root filesystem facts:

| kind | when | create cost | change detection | apply |
|---|---|---|---|---|
| **readonly+scratch** | default when the request is `shell=read` (investigate/test), any tree | O(1) | only `/scratch` and `/tmp` are writable; artifacts are listed like `coder-out` | copy chosen artifacts on request |
| **git-worktree** | the root is a git work tree and `shell=write` | O(tracked files) checkout; `git worktree add` is cheap on ext4 | `git -C wt add -A && git diff --binary <base>` → `changes.patch` | `git apply --3way --check`, then apply onto the user's tree |
| **overlay** | non-git (or git with large ignored dirs) and the probe's copy-up test passes on that filesystem | O(1) | walk the upper (O(changed)), whiteouts are deletions | copy files and apply deletions after a base-hash conflict check |
| **reflink** | overlay fails and the filesystem supports `FICLONE` (XFS `reflink=1`: **`backup_models`**, btrfs, APFS `clonefile`) | O(files in the *writable scope*) metadata, zero data | compare against a manifest captured at clone time `(rel, size, mtime_ns, ino)`, hashing only when size and mtime are ambiguous | same as overlay |
| **copy** | last resort (Win10 NTFS non-git without Detours) | O(bytes), capped (fork limits: 5,000 files / 128 MiB, `snapshot.ts:45-49`) | hash manifest | same |

**Git worktree details.**

- **Base:** a synthesized snapshot commit of the user's current state,
  including untracked non-ignored files. Port the fork's three-parent stash
  commit (`checkpoint-hooks.ts:160-217`). It is kept reachable under
  `refs/claude-hooks/council/<sid>/base`, never in `refs/stash`, mirroring
  `refs/cline/checkpoints/` (`:397-403`).
- **Create:** `git worktree add --detach --lock --reason "council <sid> lane <n>"
  <store>/wt <base>`. `--lock` prevents `git worktree prune` from deleting a
  live one.
- **Ignored heavy directories** (`node_modules`, `.venv`, build outputs), found
  with `git status --ignored --porcelain=v2 --directory`: by default these are
  **ro-bound from the original into the worktree path** inside the sandbox
  mount namespace, so tests can run. Alternatively `ignored_mode=overlay`
  makes them writable but private.
- **Discard:** `git worktree unlock`, then `git worktree remove --force`, then
  `git update-ref -d refs/claude-hooks/council/<sid>/*`, then
  `git worktree prune`.

**Overlay details.**

- Mounted **inside the sandbox mount namespace only**. Lower = the real root
  (ro). Upper and work live on the store:
  - **As root:** a per-lane **sparse ext4 image** (`fallocate` to the lane
    quota, then `mkfs.ext4 -q -O ^has_journal`, loop-mounted *inside* the
    namespace). This gives a hard disk quota with no `prjquota` mount option
    (neither filesystem here has one mounted). Delete = unlink one file. Real
    `trusted.*` xattrs work, so there is no "failed to set xattr" problem.
  - **Unprivileged:** a private tmpfs upper as in the fork, `inode32`,
    size-capped and charged to the memcg. Reconcile the upper to the store at
    command end, in the fork's `overlay.rs` style.
- Options: `xino=off,index=off,metacopy=off,redirect_dir=off`. **`xino=off` is
  the EOVERFLOW countermeasure (section 3.1, to be confirmed in P0).** If the
  copy-up probe still fails on a filesystem, that filesystem is marked
  `overlay: unusable` and the ladder falls to reflink, then readonly+scratch.
  It never falls to ptrace.
- Persistence across commands: the image (root) or the reconciled upper
  (unprivileged) is the lane's state. Each command remounts it, so a lane's
  second command sees its first command's changes.

**The huge non-git tree (`backup_models`).**

- The default request there is `shell=read` → readonly+scratch, which is free.
- For `shell=write`, the overlay is O(1) if the XFS copy-up probe passes
  (XFS `inode64` lower combined with `xino=off`). Otherwise use **reflink of
  the declared writable scope only**: `--write-scope scripts/foo/`, or the
  directories the lane names in its first write. The rest stays ro-bound.
  Never clone 136 GiB of metadata blindly. The scope is required for reflink
  views over trees larger than `reflink_max_files` (default 50k), and the tool
  error says how to name one.

**Store location and layout.** The store is
`<root>/.claude-hooks/consultants/<sid>/lanes/<lane>/`. That is gitignored
already (`.gitignore:52,57`), sits on the **same filesystem as the tree** (which
reflink and overlay need), and keeps the 96%-full `/` out of it. The layout:

```
<sid>/sandbox.json                     # state machine, per-lane records, budgets, created_at, last_used
<sid>/lanes/<n>/view.json              # kind, base ref/manifest, root, uid, scope
<sid>/lanes/<n>/{wt|upper.img|upper|clone|scratch}/
<sid>/lanes/<n>/commands.jsonl         # argv, cwd, exit, killed_by, wall, changed-paths delta (911d435b3-style)
<sid>/lanes/<n>/changes.patch | changes.json
```

### 5.5 Lifecycle, quotas, reaping and crash recovery

State machine per lane view:

```
creating → active → frozen   (lane finished; changes captured, mounts gone)
                       ├→ held      (non-empty change set awaiting review)
                       │     ├→ applied   → deleted
                       │     ├→ discarded → deleted
                       │     └→ expired (TTL) → deleted   [recorded, never silent]
                       └→ empty → deleted immediately
```

**Quotas.**

- Per-lane disk: 2 GiB, a hard cap via image size, tmpfs size or
  reflink-scope accounting.
- Per-council: 8 GiB. Global across all sids on a filesystem: 32 GiB.
- **Free-space floor:** refuse to create a view when the filesystem's free
  space is below max(10%, 20 GiB). The refusal is an `error:` string, so the
  lane reroutes to read-only.
- Inodes: refuse when free inodes are below 5%.
- Commands: memory, pids and CPU through the cgroup, time through the wall
  clock.
- The global budget is checked against the sum of `sandbox.json` records
  across sids, not by walking.

**Reaping** (four triggers, one function `sandbox_gc(now, dry_run)`):

1. At council end (`_settle_inflight` path): freeze the views; empty → delete.
2. The engine's periodic idle reaper (`_reaper_tick`, `app.py:2131`): held
   views older than `held_ttl` (default 7 days) expire. Their patches are first
   moved to `<sid>/expired/` with a 30-day retention, so expired never means
   lost.
3. Engine startup, next to `resume_inflight_councils`:
   - orphan sweep: a lane directory whose sid is neither in-flight nor in
     `sessions_index`, or that has no `sandbox.json`, is deleted after a 1 h
     grace;
   - `git worktree prune` in every repo that has
     `refs/claude-hooks/council/*`, plus deletion of refs whose sid is gone;
   - `losetup -j` sweep for images, defensive only, since the loops live inside
     dead namespaces and auto-clear.
4. Explicit: `claude-consultants sandbox gc [--dry-run]` and `sandbox ls`
   (sizes, states, ages). `verify_deploy.check_sandbox` WARNs when held views
   exceed 50% of the global budget or any orphan is present.

The `coder-out/` and per-sid directory GC gap (2.6) is closed by the same
function.

**Crash recovery.**

- Engine killed mid-command: `PDEATHSIG` plus the cgroup take the tree down.
  The kernel destroys the mount namespace and its overlay and loop mounts,
  which is why nothing is mounted in the host namespace. The view's on-disk
  state survives.
- On restart, durable councils resume (`.claude/rules/consultants-runtime.md`
  "Councils are durable"). `sandbox.json` is keyed by (sid, lane_idx), so the
  resumed lane re-attaches its view.
- The interrupted command is reported to the model as `error: command
  interrupted by an engine restart; its partial effects remain in your view` and
  is **never re-run automatically**, because commands are not idempotent.
- A launcher crash after the command exits but before reconcile (unprivileged
  tmpfs mode only): the reconcile is lost, so the lane is told so and the view
  stays at its last reconciled state. The root image mode has no reconcile
  step, which is one reason to prefer it on root hosts.

### 5.6 The shell tool, the ladder and classification

Tool `run_command`, served by `ShellToolProvider(prefix="")`, one instance per
lane:

```
run_command{ argv?: [str], command?: str, cwd?: str (relative to the view root),
             timeout_s?: int (≤ cap), write_scope?: [str] }
→ "exit=0 wall=1.8s killed_by=none\n--- stdout (n chars) ---\n…\n--- stderr ---\n…\n--- changed in your view since last command ---\nM src/x.py\nA build/out.txt"
```

- Output is capped at 20k chars, head and tail, like `git_provider.py:48`.
- The changed-path delta comes from the view, not from parsing (the
  `911d435b3` lesson).
- `is_read_only()` is False, so taint escalation applies (`policy.py:195`).
  `taints()` is False. With `net=proxy`, the first response taints.

**Ladder, given a strong sandbox.** Plan `:223-235` made the pattern table the
safety boundary because there was no sandbox. Here the sandbox is the boundary,
and the table only classifies **cost and reach**:

| class | examples | rung |
|---|---|---|
| investigate | `ls cat head rg find wc file stat du git log/status/diff python -c (net=none)` | `auto` |
| test/verify | `pytest`, `npm test`, `cargo test`, `go test`, `make check`, `ruff`, `mypy` | `auto` (they cannot escape the view) |
| build / long | `make`, `cargo build`, `npm install` (needs `net≠none`), anything with `timeout_s` > 120 | `ask_assistant` |
| egress | any command when `net=proxy` | `ask_assistant` (+ taint) |
| host network | `net=host` | `ask_human` |
| spend | `runpod`, `aws`, `gcloud`, `az`, `kubectl`, `docker run` (blocked anyway without a socket), paid CLIs | `ask_human` (or `deny` by default) |
| unmatched | — | `ask_assistant` (plan `:234`) |

The taint +1 rung applies on top. `ask_human` covers net=host and spend only, so
the human queue stays human-sized (the tool_approval coalescing and grants still
apply).

Prerequisite fixes before shell lands:

- **(Finding A)** pass `runtime_permissions=lambda: state.runtime_control.get(
  "tool_permissions")` through `as_executor`, read per call so `POST /control`
  can set `run_command: deny` on a live council.
- **(Finding B)** move `control.py:55` to the four-level ladder, accepting
  `allow→auto` and `ask→ask_assistant` as deprecated aliases.

### 5.7 Lane binding, file tools and the read cache

- **Bind per lane.** In `_wrap_researcher` / `_wrap_tool_executor`
  (`graph.py:407-430`, `:510-530`), wrap `deps.tool_executor` with
  `LaneBoundExecutor(state["lane_idx"] or a per-item id, sid)`. The shell
  provider and the view manager key on it. The bare `(name, args, cwd)`
  signature stays for every other caller.
- **File tools must see the lane's view once it exists**, just as the fork's
  overlay is shared by tools and shell (`agent-sandbox.ts:1-14`). Otherwise
  `read_file` after `run_command` shows the real tree and the model reasons
  from stale content. For a lane with a view, `read_file/list_files/glob/grep`
  resolve under `merged`:
  - for worktree, reflink and copy views that is a real directory;
  - for an overlay view, a lane-private overlay directory is only visible
    inside its mount namespace, so a long-lived "view server" sandbox process
    per lane answers reads. Simpler: run the read tools through the launcher
    with `ro` view access. Decision for P3.
- **`tool_cache`:** add a third category, `ISOLATED_EFFECT_TOOLS =
  {"run_command"}`. They **do not clear** the shared real-tree cache. Reads for
  a lane that has a view get the view id in their cache key, or bypass the
  cache. Otherwise every command flushes all lanes' caches (`tool_cache.py:19`,
  `:95-106`).
- Citation linter: claims citing a lane-view path cite a file that does not
  exist in the real tree. Tag them `evidence: lane-view <n>` so they are not
  flagged as fabrications (plan `:362-365`).

### 5.8 Enablement: off by default, on request, always-knob, addable in a follow-up

Config (`ToolsConfig` gains a `shell` sub-block; template `consultants/config.py:645`):

```toml
[tools.shell]
default   = "off"        # "off" | "always"   ← the user's "enable always" knob
mode      = "read"       # default when enabled: "read" (readonly+scratch) | "write" (private writable view)
net       = "none"       # "none" | "proxy" | "host"
roles     = ["researcher", "tool_executor"]
backend   = "auto"       # "auto" | "launcher" | "bwrap"
required_layers = ["userns","mountns","pidns","netns","cgroup","nnp","seccomp"]
held_ttl_days = 7
lane_disk_gib = 2
council_disk_gib = 8
global_disk_gib = 32
ro_binds  = []           # toolchains to expose read-only
```

The per-request override wins over config for that run only and is never
persisted to config.

- **CLI:** `claude-consultants consult … --shell[=read|write] [--net none|proxy]
  [--write-scope PATH]…` and the same on `follow-up`. Plus `--no-shell` to
  revoke for the rest of a consultancy.
- **User knob:** `claude-consultants config set-tools --shell-default always|off
  --shell-mode … --shell-net …`, **and** the same in the `/consultants config`
  menu (`feedback_configurable_means_skill_cli`; the existing tools dialog is at
  `SKILL.md:1066-1134`).
- **Runtime add in a follow-up** (the requirement). The follow-up body gains
  `"tools": {"shell": {"enabled": true, "mode": "write", "net": "none",
  "write_scope": [...]}}`.
  1. `app.py` validates it next to `extra_roots` (`:917`).
  2. It is stored on the child `SessionState` and in `runner_input` (`:1008`)
     and the `inflight.register` record (`:1024`), so a restart resumes with
     the same surface.
  3. It is **banked on the consultancy root** like `extra_granted` (`:976`), so
     later follow-ups inherit it until `--no-shell`. That is consistent with
     taint being consultancy-scoped (plan `:450-455`).
  4. `run_follow_up` passes it to
     `build_tool_surface(cfg, …, shell=resolved)` (`runner.py:825`), which
     appends `ShellToolProvider`.
  5. The follow-up's researcher system prompt gets one line: "a sandboxed
     `run_command` tool is now available (view: …, network: none)".

  The parent run is untouched. Tool specs change between runs, never within one.
- **Who requests it.** The skill decides `--shell` when the question needs
  execution ("does the test pass", "reproduce", "profile"). It must tell the
  user it did so, in one line. The review loop may add shell on an auto
  follow-up only if the user enabled `default=always`, or approved shell earlier
  in this consultancy. Otherwise it asks first. The CLAUDE.md
  confirm-before-destructive rule does not apply to enabling a sandboxed shell,
  because it is not destructive. It does apply to **apply**.
- Preflight: `claude-consultants preflight` (plan M-E.1) reports shell
  readiness (`council-sandbox probe`). Requesting shell on a host that fails
  `required_layers` **stops and asks** (fix / run without shell / cancel). It
  never silently degrades (plan `:266-275`).

### 5.9 Council finished → what Claude Code sees and does

Mirrors the fork's hand-back (`delegated-sandboxes.ts:409-451`) and its "nothing
is auto-applied" rule (`57fc361b0`).

- The final answer gains a **"Sandbox changes"** section. Per lane it lists:
  the view kind; files A/M/D with ±lines; the patch path; commands run with
  exit codes (top 10, then a count); and the state `held`. It starts with:
  "Your files are UNCHANGED. These changes are held in the lane's private view."
- `status <sid>` gains `sandbox: {lanes:[{lane, kind, state, files, bytes,
  patch, expires_at}], held_bytes, budget}`, and the consultancy status shows
  `held_changes: N`.
- Verbs (the CLI is the authority, and the skill drives it):
  - `claude-consultants changes <sid> [--lane N] [--diff|--stat]`
  - `claude-consultants apply <sid> --lane N [--paths p…] [--dry-run]`.
    Worktree: `git apply --3way --check`, then apply. Overlay, reflink and
    copy: per file, require the user tree's current hash == the base hash
    recorded at view creation. A mismatch is listed as a conflict and skipped
    unless `--force`.
  - `claude-consultants discard <sid> [--lane N|--all]`
  - `claude-consultants sandbox ls | gc [--dry-run]`
- **Skill flow after `accept`:** if `held_changes > 0`, show `changes --stat`
  and ask the user to apply, discard or keep. Apply only on an explicit yes,
  because it is a destructive edit to the user's tree. Keeping means "expires in
  7 days", and the skill says so. A consultancy accepted with held changes stays
  listed in `sandbox ls` until applied, discarded or expired.

### 5.10 Windows and macOS parity

| layer | Linux | Windows (pandorum: Win10 19045, NTFS) | macOS |
|---|---|---|---|
| process jail | userns + PID ns | **AppContainer** (`CreateAppContainerProfile`, no capabilities) + **Job Object** (`KILL_ON_JOB_CLOSE`, `ACTIVE_PROCESS_LIMIT`, `JOB_MEMORY`, `CPU_RATE_CONTROL`, `UILIMIT_*`, breakaway disabled) | Seatbelt (`sandbox-exec`, deprecated but shipped: the fork relies on it, `macos.rs:195-198`) + process group + `setrlimit` |
| writes | rw only the view | AppContainer has no write access except ACL-granted paths. Grant the lane view and private temp. Fallback **Low integrity** (fork `windows.rs:201-405`) where AppContainer breaks a toolchain. | `(deny file-write*)` except the view, private tmp and `/dev` (the fork's profile, tightened) |
| secret reads | not mounted | AppContainer cannot read the user profile by default. The fallback Low-IL **can**, which is a documented gap. | add `(deny file-read* (subpath "~/.ssh") …)` for a secrets list |
| network | netns `lo` | AppContainer without `internetClient` / `privateNetworkClientServer` blocks it (WFP). The Low-IL fallback cannot block it without admin WFP filters, so shell there is `net: unenforced` and requires `ask_human` for every command, or is withheld (config). | `(deny network*)` |
| limits | cgroup | Job Object | `RLIMIT_*` only, no memory cgroup. Wall clock and process group kill. |
| view, git | worktree bound at the real path | worktree at a sibling path. **No path fidelity**: cwd + argv rewrite as the fork's M1 does (`macos.rs:247-271`). | same as Windows |
| view, non-git | overlay / reflink | **Detours overlay** (reuse the fork's `hook.dll`, which keeps absolute paths) or capped copy. No block cloning on Win10 NTFS. | `clonefile` (the fork's M1) |
| spawn | — | `windowless_python_executable()` / `CREATE_NO_WINDOW` (`feedback_windowless_python_for_daemons`) | — |

Windows and macOS are weaker than Linux, and the probe output says which layers
are enforced. `required_layers` defaults per OS:

- Windows: `appcontainer`, `job`, `net`.
- macOS: `seatbelt`, `net`, `rlimit`.

A host that cannot meet them withholds the shell.

### 5.11 Launcher as a deploy artifact

- **Source:** `sandbox/council-sandbox/` in claude-hooks. Rust, no crates, with
  the per-OS modules of the fork. Read the fork's `confine.rs`,
  `overlay.rs`, `macos.rs` and `windows.rs` as references; port them, do not
  vendor them.
- **Build:** a CI workflow modelled on `sandbox-binaries.yml`, with one
  change: **Linux targets are `x86_64-unknown-linux-musl` and
  `aarch64-unknown-linux-musl`, fully static.** The fork's glibc-2.34 binary
  does not run on bullseye (section 4). Windows: `+crt-static`
  (`windows.rs` notes), with `hook.dll` only if the Detours view is used.
  macOS: universal or two binaries, ad-hoc codesigned.
- **Distribution:** a GitHub Release asset with a committed
  `config/sandbox-binaries.json` (name → sha256), the same pattern as the
  v1.4 llamafile asset fetched by `install.py`. Never committed binaries in
  `bin/`.
- **`scripts/deploy.py`:** a new `step_sandbox` **before `step_services`**:
  1. fetch or verify the sha256;
  2. install to `~/.claude/claude-hooks-bin/council-sandbox[.exe]` with mode
     0755;
  3. run `council-sandbox probe --json` and write
     `~/.claude/council-sandbox-probe.json`;
  4. on Windows, ensure the AppContainer profile exists;
  5. on Ubuntu ≥ 24.04 non-root hosts, install the AppArmor userns profile
     (`/etc/apparmor.d/council-sandbox`, needs sudo, interactive only per
     `feedback_install_destructive_noninteractive`).
- **`scripts/verify_deploy.py`:** `check_sandbox` FAILs when the binary hash
  does not match the manifest while shell is enabled anywhere in config, or when
  a **real smoke run** fails. The smoke run executes `run` with `/bin/true` in a
  temp readonly view and then asserts:
  - a write outside the view fails;
  - reading `$HOME/.claude.json` fails;
  - `connect(1.1.1.1:443)` fails with `net=none`;
  - a fork bomb is capped by pids.

  It reports INFO for optional layers that are absent (Landlock). It uses the
  interpreter the engine uses ("configured" ≠ "working", v1.14 lesson).
- **`tests/test_deploy_completeness.py`:** add the artifact class first (rule
  in `.claude/rules/deploy.md`), asserting `step_sandbox(` precedes
  `step_services(` and that `check_sandbox` exists.

---

## 6. Phased plan

Each phase ships its own `config` verb **and** skill menu section, plus an M12
cohort-2 parity test proving that with shell off the default behaviour is
byte-identical.

**P0 — prerequisites and measurement (S, ~350 LOC).**

- Finding A (runtime_permissions plumbing) and Finding B (control enum →
  ladder, with aliases).
- `LaneBoundExecutor` with lane identity, still unused.
- `ISOLATED_EFFECT_TOOLS` category in `tool_cache`.
- `scripts/sandbox_probe.py`: a Python, read-mostly host matrix covering
  namespaces, cgroup delegation, the seccomp actions, Landlock ABI, FICLONE on
  each root's filesystem, an overlay copy-up test under `{xino=auto, xino=off,
  tmpfs inode32}` on the repo ext4 and on the XFS of `backup_models`, bwrap
  version and free-space floors.

Exit: the EOVERFLOW hypothesis is confirmed or refuted on solidpc, and the
results land in this doc.

**P1 — Linux readonly shell (M, ~900 LOC including tests).**

- `ShellToolProvider` with only the `readonly+scratch` view.
- Backend `bwrap` (0.4.1, `--unshare-all --unshare-user --uid 65534
  --die-with-parent --new-session --cap-drop ALL --seccomp <fd>`, with the
  seccomp BPF from a tiny static helper), wrapped in a `systemd-run --scope` for
  limits.
- `--shell[=read]` on `ask` and `follow-up`; the follow-up body field,
  persistence and the inflight record; the `[tools.shell]` config, CLI verbs
  and skill menu; the ladder table.

Exit: an escape test suite (5.11 list, plus ptrace of a host PID, `mount`,
`unshare`, `io_uring`, writes to `/dev/*`) is green. A follow-up adds shell to a
consultancy that started without it, survives an engine restart mid-command,
and reports the interruption.

**P2 — the `council-sandbox` launcher plus git-worktree write views (L, ~1,800
LOC).**

- The Rust launcher (Linux layers 1–10, `probe`, JSON spec on fd 3 and result
  on fd 4), musl CI, release asset, `step_sandbox`, `check_sandbox`.
- Git-worktree view, store layout, state machine, `sandbox.json`, reaper (all
  four triggers), the `changes/apply/discard/sandbox ls|gc` verbs, the final
  answer's "Sandbox changes" section, and the skill's post-accept apply dialog.
- Lane-view file-tool routing for worktree views.

Exit: no stale worktree after kill-9 of the engine at every lifecycle state; a
budget-exhaustion test; `apply --3way` conflict tests.

**P3 — non-git write views (M, ~900 LOC).**

- Overlay (root: sparse-image upper; unprivileged: tmpfs plus reconcile) with
  the P0-validated options.
- Reflink view with a required write scope above `reflink_max_files`; the copy
  view.
- The file-tool routing decision for overlay views.

Exit: a bench on `backup_models` (create view, run `python scripts/x.py` that
writes outputs, collect changes) with view creation under 2 s and no
data-proportional disk use.

**P4 — Windows and macOS (L, ~1,500 LOC).**

- Windows: AppContainer + Job Object, worktree view, optional Detours overlay
  view (port of the fork's `hook.dll`), Low-IL fallback with `net:
  unenforced` gating.
- macOS: Seatbelt + rlimits + clonefile.
- Smoke on pandorum **before** a release tag (`feedback_release_test_pandorum_first`).

**P5 — egress and hardening (M, ~600 LOC).**

- `net=proxy` through the M-F network provider's allowlist, with taint on first
  response.
- The Landlock layer when active, and the documented boot-parameter step.
- The Ubuntu 24.04 AppArmor profile artifact.
- An optional `claude-council` system uid on root hosts in place of 65534.

**Later (P6).** A VM backend (Firecracker or gVisor `runsc`) for `paranoid`
mode.

---

## 7. Risks and open questions

1. **Kernel attack surface.** A userns sandbox exposes the kernel's
   userns-reachable code; most local privilege escalations of 2022–2024 went
   through userns plus nftables or overlay. Mitigations: seccomp denies
   `unshare`/`setns`/`CLONE_NEW*` (no nested userns, so no nft or mount inside),
   and there is no network namespace configuration. The residual risk is a VM
   backend's job (P6). The 6.2 PVE kernel is the bpo build, so its patch level
   matters.
2. **Root engine.** The engine runs as root without unit hardening. A launcher
   bug before `setuid(65534)` runs as root. Keep the pre-exec code small,
   no-crates and fuzzed. Consider moving the consultants unit itself under
   `ProtectSystem=strict`, as the daemon already is.
3. **EOVERFLOW is unconfirmed.** If `xino=off` does not fix it, ext4 overlay is
   lost on solidpc. Worktree (git) and reflink (XFS) still cover the stated
   needs, and readonly+scratch covers the rest.
4. **Ignored directories in worktrees.** A ro-bound `node_modules` means
   `npm install` cannot modify it. That is intended for `shell=read`;
   `ignored_mode=overlay` serves write builds.
5. **File tools vs overlay views.** Lane-private mounts are invisible to the
   engine. Routing reads through the launcher costs a spawn per read. The view
   server is more code. This is decided in P3 with a measurement.
6. **Classifier drift.** The table is now a cost and egress classifier, so a
   wrong `auto` costs CPU time inside a jail, not a host compromise. Unmatched
   still goes to `ask_assistant`.
7. **Disk on solidpc.** `/` is at 96% and must never hold views. The store is
   per root on the root's own filesystem, with a free-space floor. A wrongly
   configured `store_root` must FAIL verify, not warn.
8. **Windows network.** Without AppContainer (toolchain ACL breakage) the
   network cannot be blocked unprivileged. That is a policy choice (withhold,
   or `ask_human` per command), surfaced in preflight and never hidden.
9. **Follow-up inheritance.** Inheriting shell across a consultancy is a
   convenience that widens later follow-ups silently. The answer's provenance
   line must state "shell: enabled since follow-up #k (mode, net)".
10. **The glibc trap.** Any dynamically linked launcher repeats the fork's
    bullseye failure. CI must assert `file` reports "statically linked" for the
    Linux binaries.
11. **Plan doc drift.** The `PLAN-council-tool-surface.md` status header
    (`:3-12`) still says the approval channel is missing; it is built
    (`tool_approval.py`). Update it when P0 lands.

---

## 8. Appendix — relevant fork commits since 2026-08-12

The fork mostly uses a bare `scope:` prefix (for example `tools:`) rather than
conventional-commit parentheses. This list was filtered from `git log
--since=2026-08-12` on the scopes sandbox, terminal(s), tools, safety, guards,
revisions, check(_file), runtime, host and atomic, plus agents, approvals and
fix commits that touch the sandbox, shell or approval paths.

**Sandbox and launcher**

- `e9f240477` 10-04 sandbox: confine agents' commands by default, the lead's on request
- `d8b916bcc` 10-04 sandbox: direct mode, confinement with no overlay, for the lead's commands
- `a59bd5af1` 10-04 sandbox(windows): compare a handle's path in the workspace root's own spelling
- `b80de2a38` 10-04 sandbox(windows): a relative write in the working folder goes to the overlay
- `191df32e7` 10-04 sandbox: write confinement in the launcher, on every backend
- `748430786` 09-29 sandbox: log only workspace and overlay opens; keep the log as .gz
- `8475cc024 7aa0b7507 d7353274e 22c4efc60 bf628d8b9 8cd12a3ae` 10-04, `b8e4d4831` 09-29, `7c52a3675` 09-24 assets: refresh command-sandbox binaries from CI
- `b3ff2d55a` 09-24 ci: auto-commit the six command-sandbox binaries built on native runners (#88)
- `821de4a3b` 09-24 docs: how the command-sandbox binaries are built (CI + pandorum) (#87)
- `c19f17c47` 09-24 agents: fold the Windows launcher into the Rust binary (W1), build in CI (#86)
- `0567012a7` 09-24 agents: macOS M1 sandbox backend (APFS clonefile), CI-verified (#85). The first commit of `linux.rs`, `linux_l2.rs` and `resolve.rs` in history.
- `2b9561e55` 09-24 agents: resolve the folded Windows launcher + macOS launchers by name (#89)
- `656841fe3` 09-24 agents: repair two tests the command-sandbox work left failing (#90)
- `57fc361b0` 09-24 agents: delegated sub-agents run in a private overlay and hand changes back as revisions
- `614b80752` 09-26 agents: /delegate and background delegations run in the sandbox
- `c7934a647` 09-25 fix(core): executors never show a sandboxed agent its overlay path
- `e8c0e7829` 09-25 fix(core): a refused respawn leaves the running teammate's overlay alone
- `3517e6569` 09-25 fix(core): a sandboxed agent can edit a file it read
- `3c4c9564a` 09-25 history-ux: … delete purges overlays (#93)
- `626a68180` 09-25 release: 4.100.196 (every delegated agent sandboxed)
- `d6c062ffc` 10-04 agents: a check whose program the shell cannot find is not the agent's to fix
- `6c3c3fa58` 10-09 agents: a withheld spawn_agent says why
- `102e08f5f` 08-13 fix(core): reclaim idle plugin sandbox processes (#13227)
- `c03e45231` 08-24 fix(sdk): carry root overrides into the Node smoke-test sandbox (#13517)

**Revisions and held changes**

- `ea01be246` 10-04 agents: held revisions are not the file …
- `791e9c9a5` 09-26 revisions: a lead that can delegate can reach its agents' work with Checkpoints off
- `2459f758e` `f331d6af2` `4a0db2d22` 09-16 revisions: compaction hand-off, two-compaction retention, a cap across files
- `198e442ac` 09-16 compaction: tell the revision log which histories survive it
- `9fa1d56a5` 09-18 feat(atomic): anchor a revision to the file, not to the transaction
- `9d6dd4ea6` 09-14 atomic: a revision the model can choose between
- `2c8997b25` 09-13 atomic: a file has a history, not just an original
- `5075dadc0` 09-14 restore_file: say which revision it went back to
- `d4e17a14a` `4f983467e` 09-05 tools: an undo the model can reach / the transaction already had the original
- `7902ed023` 08-17 atomic: the oracle and the rollback the protocol is built on (introduces the snapshot)
- `b57cf386e` 08-17 core: snapshot the workspace, not the working directory

**Terminal and shell**

- `fc6929a2b` 10-09 terminal: end a command whose end VS Code never reports
- `acfda70f7` 10-09 terminal: an unfinished command at the >> prompt is reported, not waited on
- `cacac5d55` 10-09 release notes: the unfinished shell command
- `431eb4522` 09-10 terminals: an abandoned terminal closes when it is abandoned
- `3c6554791` 09-08 terminal: a window title is not command output
- `233c60db4` 09-05 terminal: a command that printed nothing is not a command we failed to read
- `911d435b3` 09-16 tools: ask the filesystem what a command changed, not the command line
- `b279496e5` 09-16 tools: one writer at a time, because there were none
- `f2d9ecbaf` 09-16 tools: notice when somebody else wrote the file
- `3c433d5a9` 08-18 fix: run_commands object form without args routes through the shell (#13336)
- `d9bb22883` 08-19 fix(shared): run PowerShell commands with fail-fast error semantics (#13358)
- `b136194ce` 09-15 check_file: JSON.stringify is not Windows shell quoting
- `c017c7016` 08-27 fix(desktop): make the Tauri shell work on Windows (#13632)
- `d7d513010` 09-12 tools: keep sed out of the presets that turned the editor off
- `df768a961 66a9dffbc a1ed21493 d40489868` 09-12 tools: in-process grep / sed / awk (the shell-free path)

**Tools, read-only and approvals**

- `9f20d6937` 09-26 xollama: mark read-only tools x_read_only for the council
- `461c738aa` 09-26 tools: the runtime catalog and the shipped signatures carry the lead's agent tools
- `d243adbc8` 10-04 tools: log each call's arguments as sent
- `0a9209458` 09-14 approvals: put them in the chat, where markdown renders and a no can say why
- `099a6e242` 09-13 auto-approve: grep, sed and awk were in no domain, so nothing ever asked
- `f267ccc92` 09-10 mcp: the auto-approve grant is reachable again, and it matches the right tool
- `2b7b01328` 08-23 fix(vscode): auto-approve all MCP tool calls when the MCP toggle is on (#13498)
- `833cc891b` 08-24 chore(vscode): hide per-tool MCP auto-approve checkboxes behind a flag (#13522)
- `fb8eb79d2` 09-21 vscode: the auto-approve preview linger must not stall the agent loop
- `442d9cabe` 09-05 cli: a batch has nobody to ask, so let it approve the check itself
- `513db5289` 09-20 runtime: a batch of tool calls runs as a batch
- `9aad7958d` `a99c86b8e` `e77fa2b22` 09-05 runtime: prose about a tool call / an unreadable call / nudges
- `916c4aac7` 09-13 tools: a host that brings its own reader keeps the read guard
- `8338c7a75` 08-17 host: the session's record keeps the user's words

**Safety, guards and checks**

- `f76f0d55d` 09-18 safety: the revision number was hiding the loop from the guard
- `5fc435627` 09-17 safety: the struggle detector reads the signal the protocol emits
- `0fa66bef2` 09-16 guards: the nudges fired in silence, so their thresholds were the untuned ones
- `ff88c806e` 09-23, `bbf00f193` 09-13, `e17c436ab` 08-17 loop guard: …
- `7edbfab26` 09-13 escalation: spend a terminal guard on the expert, once
- `94807fa21` 09-13 escalation: the budgets and the approval, reachable from the panel
- `b73bed123` `86f9b786b` `0687257de` `8c204d7b6` `6bf9438cb` `53fd96c5c` 09-04..09-10 atomic: the model proposes the check, the user approves it; an approved check is the only one that counts; …
- About 45 further `atomic:` commits (08-17 → 09-13) on the transaction protocol, the oracle and the plan tool. They are relevant only through `snapshot.ts` and `file-revisions.ts`, which are covered above.
