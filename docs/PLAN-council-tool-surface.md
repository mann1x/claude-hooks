# PLAN — council tool surface: MCP bridge, network, shell, git, uniform roles

Status: **scoping draft**, 2026-08-01. No code written.

Scope request (2026-08-01): (1) generic MCP bridge, fully configurable
including the config menu; (2) direct network access, configurable,
default off; (3) all roles get the same tool access; (4) read-only
pattern-gated shell, with a permission channel that pauses and asks the
assistant/user before builds, expensive, or destructive work; (5) git
history tools that answer "when did this regress and why"; (6) the
config surface must cover all of the above; (7) a preflight gate so a
council refuses to start silently degraded when a picked MCP server is
down, failed, or needs re-auth.

---

## 0. What already exists (verified, not assumed)

Three findings change the shape of this work.

**The runner is registry-free by design.** `claude_hooks/agent_loop/runner.py`
states it "has no hardcoded HTTP client or tool registry" — the caller
passes `tool_specs` + a `(name, args, cwd) -> str` executor. `GraphDeps`
(`consultants/engine/graph.py:193`) already carries both as injected
fields. The council's entire surface is therefore decided at exactly two
call sites, `consultants/server/runner.py:278` and `:725`, both passing
`caliber_proxy.tools.openai_tool_specs()`. **Replacing the registry is a
one-place change**, not an engine refactor.

**The permission channel is built but unwired.**
`consultants/engine/interrupt_policy.py:228` defines
`should_interrupt_on_tool_permission`, documented as *"a tool the
researcher or tool_executor wants to call has its permission set to
`ask` in `runtime_control.tool_permissions`. Pause at the call site,
surface the args, wait for human approval."* It is unit-tested
(`tests/test_consultants_v2_interrupt_policy.py:209-250`), its
`allow`/`deny`/`ask` values are validated by the control API
(`consultants/server/control.py:349`) — and **no node calls it**. A
repo-wide grep for `should_interrupt` outside tests finds only the
*other* three policies.

This repeats a shape the project has seen before and already closed: the
LSP engine shipped complete with tests in v0.7 and sat uncalled until
v1.9 wired it into the hooks (`634d6d2`, #244). That one is **history,
not a live defect** — `session_start.py:84`, `post_tool_use.py:363`, and
`session_end.py:76` all call the helpers today. The point is only that
"complete policy, complete tests, zero wiring" is a failure mode with
precedent here, and `should_interrupt_on_tool_permission` is currently
in that state.

Everything downstream of that interrupt already ships: the SSE event
stream (`consultants/server/events_sse.py`), the resume/interrupt
control routes (`control_routes.py`), the durable checkpointer, and an
`awaiting_approval` consultancy status from the v1.12 follow-up cap.

**Config has a template to copy.** The `[store]` block —
`StoreConfig` (`consultants/config.py:547`), three CLI verbs
(`cmd_config_set_store{,_ttl,_distillation}`, `cli.py:797-856`), and a
skill dialog section (`.claude/skills/consultants/SKILL.md:579`) — is the
worked example for adding a configurable subsystem.

### Today's surface, for the record

Six tools, shared verbatim with `/get-advice`:
`survey_project`, `list_files`, `read_file`, `glob`, `grep`,
`recall_memory` (`caliber_proxy/tools.py:471` `TOOL_IMPLS`). Plus
`write_file`, scoped to the opt-in `coder` role.

Per-role access is the more surprising half:

| role | tools | default |
|---|---|---|
| researcher | all 6 (inline `agent_loop` subloop) | **on — the only default role with tools** |
| tool_executor | all 6 | off |
| coder | `write_file` | off |
| planner, critic, meta_critic, synthesizer, adversary | **none** (`_single_shot`) | on |

---

## 1. The unifying observation

All six requirements are the same sentence: *the council reaches
something outside its current sandbox, under a policy, configurably,
with an approval path when the action is consequential.*

So this is **one capability layer**, not six features. Building it as six
bespoke tools would duplicate the policy, the config, and the approval
plumbing four times and guarantee they drift.

### Architecture

```
                      ┌──────────────────────────────┐
  every role node ──► │  ToolRegistry                │
  (uniform surface)   │   merge specs, one dispatch  │
                      └──────────────┬───────────────┘
                                     │ every call
                      ┌──────────────▼───────────────┐
                      │  PolicyGate                  │  ◄── [tools] config
                      │  allow / ask / deny          │      + runtime_control
                      └──────────────┬───────────────┘
                            ask ─────┴──── allow
                             │              │
              interrupt() ───┘              ▼
              SSE → assistant/user   ┌──────────────────────────┐
              resume(approved?)      │ ToolProvider             │
                                     ├──────────────────────────┤
                                     │ builtin  (today's 6)     │
                                     │ git      (log/blame/…)   │
                                     │ shell    (pattern-gated) │
                                     │ mcp      (N servers)     │
                                     │ network  (http)          │
                                     └──────────────────────────┘
```

`ToolProvider` deliberately mirrors the memory-backend `Provider` ABC in
`claude_hooks/providers/base.py` — same repo idiom (`detect` / `verify` /
use), so adding a provider stays one file plus a registry entry.

**The PolicyGate sits in front of dispatch, not inside each provider.**
That is the keystone: wire the gate once and every provider added later
inherits approval, denial, and audit for free.

### The permission ladder (decided 2026-08-01)

Four levels, not the three the control API validates today
(`allow` / `deny` / `ask` at `control.py:349`). Extending that enum is a
schema change that touches the route validator and its tests.

| level | who approves | applies to |
|---|---|---|
| `auto` | **nobody — runs silently** | reads, non-destructive investigation |
| `ask_assistant` | assistant, **auto-approves**, may escalate at its discretion | writes, builds |
| `ask_human` | human, mandatory | anything that spends money (renting a pod, paid API calls) |
| `deny` | — | blocked outright |

The rationale for `auto` is explicit and load-bearing: routing a `grep`
or a `git blame` through the assistant **burns tokens for nothing**. The
gate must therefore be cheap on the common path — a dict lookup that
returns `auto` and never touches the LLM or the interrupt machinery.

`ask_assistant` is "auto-approve with discretion", not "wait for a
verdict": the assistant approves by default and escalates only when the
request looks worse than its class suggests. `ask_human` is the only
level that can stall on a person, which is why it is reserved for spend.

---

## 2. Milestones

Ordering is not negotiable on one point: **the gate lands before any new
capability.** Any other order opens a window where the council has shell
or network with no approval path.

Standing rule per milestone, per `feedback_configurable_means_skill_cli`:
every milestone ships its own `claude-consultants config` verb **and** its
`/consultants config` menu section. Config is not deferred to a final
milestone. Likewise per `project_consultants_v2_m12_parity`: every new
opt-in adds a cohort-2 parity test proving default behavior is unchanged.

### M-A — registry + policy gate (foundation, no new capability)

Extract `ToolProvider` / `ToolRegistry`; make registry composition the
single source feeding `runner.py:278/725`. Wire
`should_interrupt_on_tool_permission` at the dispatch boundary, extended
to the four-level ladder. Add the `[tools]` config block.

**Per-lane parking (decided).** An `ask_*` suspends only the asking lane;
its x-tier siblings keep running. This is the largest single piece of
M-A: today's interrupt is graph-level, so per-lane suspension needs
lane-scoped interrupt state threaded through the checkpointer — the same
territory as the M11c-3 `parent_lane_idx` work, and subject to the same
rule that N×M fanout must survive intact
(`feedback_xtier_diversity_priority`). The cheaper graph-level pause was
rejected deliberately: one permission request idling every lane is the
expensive failure on exactly the runs that matter most.

Denial and approval-denial must return a **tool result string**, not
raise — `tools.execute` already establishes that convention ("errors are
returned as `error: ...` strings so the model sees them and can
recover"). A denied tool should teach the model to try another route,
not crash the lane.

Exit: byte-identical default behavior, M12 parity green, gate provably
fires (`ask` on a builtin tool pauses and resumes).

### M-B — uniform role access (req 3)

Convert `planner`, `critic`, `meta_critic`, `synthesizer`, `adversary`
from `_single_shot` to the loop path. This is the highest-risk milestone
and deserves a blunt warning: it multiplies LLM round-trips across
**N×M** x-tier lanes, and `feedback_xtier_diversity_priority` forbids
degrading that fanout to pay for it. Needs a measured before/after on
tokens and wall time, benchmarked the way M11c was.

Upside beyond the requirement: a critic that can `read_file` can verify a
citation itself. The CitationLinter exists precisely because these roles
are blind (the fabricated `store_sql.py` came from a researcher lane with
zero tool calls and flowed unchallenged through `peer_findings`).

### M-C — git provider (req 5)

`git_log`, `git_blame`, `git_diff`, `git_show`, plus a
`when_did_this_change(path, symbol)` convenience that composes them —
the actual question being asked. Read-only by construction, confined to
the same roots as the path tools. Cheapest real capability and the best
first proof of the registry.

### M-D — shell provider (req 4)

Pattern-based allowlist for investigate/test/verify, with escalation to
the approval channel for builds, spend, and anything destructive.

Honest limitation to design around: **"read-only" is not decidable from a
command string.** `npm test` writes caches and can invoke arbitrary
scripts; `git gc` rewrites the object store. So the allowlist is a
heuristic that must fail *closed* — unmatched commands escalate rather
than run — and the escalation path is what makes the feature safe, not
the pattern list.

Each command maps to a ladder level, which is what makes the pattern list
tractable: investigate/test/verify patterns resolve to `auto` and run
without ever paying for an approval round-trip; builds and writes resolve
to `ask_assistant`; anything that provisions paid resources resolves to
`ask_human`. Unmatched → `ask_assistant` (fail closed, but to the cheap
approver, not the human).

Every escalation carries a concrete explanation: the command, the root it
would run in, why the council wants it, and which class it tripped. An
approver cannot judge `sh -c "..."` on its own.

### M-E — MCP bridge (req 1)

Generic client over N configured servers, tools namespaced per server
(`<server>__<tool>`) to avoid collisions with builtins.

**Both transports in the first cut (decided).** `claude_hooks/mcp_client.py`
gives streamable-HTTP `initialize` → `tools/call` on the stdlib for free;
**stdio is new work** — subprocess lifecycle, framing, reaping — and it
is what unlocks the bulk of the ecosystem, context7 included. On Windows
it must spawn via `windowless_python_executable()` rather than
`sys.executable`, per `feedback_windowless_python_for_daemons`, or every
server pops a console window.

**Server list is explicit, not inherited (decided).** The council keeps
its own list, seeded from `~/.claude.json` by an import command. Adding a
server to Claude Code for your own use must not silently widen what the
council can reach — which matters directly for the confused-deputy risk
below.

Deliberately generic: searxng is one server among many, not a
first-class dependency. The same bridge reaches context7 (live library
docs), the repo's own code-graph MCP, and github.

#### M-E.1 — preflight health gate (req 7)

An MCP server can be down, timing out, or need re-auth. **The failure
mode is silent degradation, and this project has been bitten by exactly
that class before**: a dead server returns nothing, which is
indistinguishable from "nothing found" — the same shape as the v1.14.0
pgvector bug, where a killed connection returned `[]` and read as an
empty corpus. A council that quietly ran without its docs server
produces a confident, ungrounded answer and no signal that it did.

So the gate is not optional polish; it is what keeps the capability
honest.

**Before** a council starts (`ask`) or a follow-up resumes, probe the
eligible servers: reachable, authenticated, `tools/list` non-empty and
containing the tools the config references. Classify the failure —
unreachable / timeout / auth-required / auth-expired / protocol error /
tool missing — because the remediation differs and a generic "failed" is
useless to the person who has to fix it.

#### What counts as eligible: the silent-skip rule (decided)

MCP servers are configured **per session**, so the council's list and the
session's reality diverge routinely. The gate must not complain about a
server that simply isn't part of this session.

```
server in council list  ∧  present in session  ∧  unhealthy  →  BLOCK + ask
server in council list  ∧  absent from session                →  skip, silently
server in council list  ∧  present  ∧  healthy                →  use
```

Only the first row stops a council. A server the session never had is not
a fault, and treating it as one would make preflight a nag that operators
learn to `--allow-degraded` past reflexively — which would destroy the
signal precisely when it matters.

This needs a mechanism, because the engine runs headless and cannot see
the session's MCP surface: **the skill is the only component that knows
which servers this session has**, so it passes them down —
`claude-consultants preflight --session-servers <csv>` — and the CLI
intersects that with the council's list. The engine never guesses.

Note the honest limit: a silently-skipped server still means the council
is weaker than its config implies. It is skipped from the *gate*, not
from the *provenance record* — the final answer still states which
configured servers were absent, without blocking on them.

On any failure the skill **stops and asks**, per the request. Three
outcomes, and the CLI is the single authority (the skill drives
`claude-consultants preflight`, it never probes on its own or edits TOML
directly — same rule as the rest of the config surface):

1. **Fix** — surface the concrete remediation per failure class, re-probe.
2. **Run degraded** — explicit opt-in, `--allow-degraded`. The set of
   missing servers is recorded on the session and must appear in the
   final answer's provenance, so a claim that would have been grounded by
   the missing server is not read as if it was.
3. **Cancel.**

Two cases deserve their own handling:

- **Follow-ups.** A follow-up reusing a parent thread whose server is now
  down is a *capability regression mid-consultancy* — the parent's
  answers were grounded by something this run cannot reach. Flag it
  distinctly from a cold start, because silently continuing makes the
  follow-up look like a peer of the parent when it is weaker.
- **Mid-run death.** A server that passes preflight and dies later must
  degrade to a tool-result error string (the `error: ...` convention, so
  the lane recovers and reroutes) and land in the same provenance record.
  Preflight reduces this window; it cannot close it.

### M-F — network provider (req 2)

Direct HTTP fetch/search, **default off**, with domain allow/deny lists,
size and timeout caps, and content-type limits.

### M-G — config consolidation (req 6)

The per-milestone verbs above unified into one coherent
`/consultants config` tools dialog + `config show` rendering, with the
per-project scope banner that `override_user_global` already drives.

---

## 3. Risks that need decisions, not just care

**Prompt inflation across lanes.** Requirement 3 (all roles get tools) ×
this many new tools = every role's every call carries the full schema
block, multiplied by N×M lanes. Mitigation: uniform *surface* per
requirement 3, but provider activation gated by effort tier, so a
`medium` run doesn't pay for MCP schemas it will never call.

**Confused deputy.** Decided — see §3.1 below. After M-E/M-F the council
reads untrusted external content and after M-D it can run commands: a
crafted page tells a researcher lane to run something, and the lane has a
shell.

**Citation linter vs new evidence types.** The linter verifies
`path:line` against the mtime-cached code graph. Web-sourced and
git-sourced claims have no such anchor and will be annotated as
fabrications unless provenance tagging is extended per evidence type.

**Sandbox parity.** `make_executor(extra_roots)` confines path tools
today; shell and git must honor the same roots or the sandbox has a side
door.

---

### 3.1 The taint invariant (decided 2026-08-01)

**Once the session is tainted, every shell command moves one rung up the
ladder for the rest of the session.** `auto` → `ask_assistant`,
`ask_assistant` → `ask_human`. `ask_human` and `deny` are already at the
ceiling and do not move.

The property that makes this worth having: after untrusted text enters
the session, *no shell command runs without some reviewer seeing it* —
but nothing jumps straight to a human either, so the cost of the common
case stays on the assistant. Enforcement is one boolean on session state;
there is no dataflow analysis and therefore no false precision. Rejected
alternatives, for the record: per-lane taint (precise, but M13 already
showed that LangGraph `Send` delivers only dict keys, so a missed
propagation edge silently drops the taint while still looking like a
guarantee) and capability separation (structurally strongest, but it
contradicts requirement 3).

Accepted cost: a clean lane pays for a sibling's fetch. That is the price
of not pretending to track dataflow.

#### When the taint fires — the two sources are not symmetric

This is the part that is easy to get wrong.

- **Network provider — taint on first result.** Nothing untrusted exists
  until a fetch returns. A council that never fetches is never tainted.
- **MCP provider — taint on activation, before any call.** An MCP
  server's **tool descriptions are third-party text that lands in the
  prompt as part of the schema block**, and the model reads them whether
  or not it ever calls the tool. So a hostile server does not need to be
  invoked to inject; being listed is enough. Waiting for a first result
  would leave exactly that window open.

The practical consequence is worth stating plainly rather than
discovering later: **any council with the MCP provider active is tainted
from turn 0**, so every shell command in such a run is gated at
`ask_assistant` or above. If that proves too heavy in practice the lever
is per-server trust marking (a reviewed server declared trusted at
import), not weakening the rule.

#### Follow-ups inherit taint

A follow-up replays the parent's message thread, which contains the
parent's fetched content. Taint is therefore a property of the
consultancy, not of a single run, and must be persisted alongside the
consultancy status rather than recomputed per run.

## 4. Decisions

All resolved 2026-08-01. Nothing in this plan is blocked on a further
decision; what remains is estimation and sequencing.

| # | Decision | Resolution |
|---|---|---|
| 1 | Who approves a gated tool? | **Four-level ladder.** `auto` for reads/non-destructive (no approval — routing these through the assistant burns tokens for nothing); `ask_assistant` for writes and builds (auto-approves, escalates at discretion); `ask_human` for spend; `deny` |
| 2 | Pause scope on an ask | **Park the lane, siblings continue.** Costs lane-scoped interrupt state in M-A; rejected graph-level pause as the expensive failure on x-tier runs |
| 3 | MCP server source | **Separate explicit list**, seeded from `~/.claude.json` by an import command. Adding a server to Claude Code must not silently widen the council's reach |
| 3b | Server configured for the council but absent from the session | **Silently skipped** — never an error. Only *present-and-unhealthy* blocks. Skill supplies the session's server list via `--session-servers` |
| 4 | MCP transport | **Both from the start.** stdio is new work (subprocess lifecycle + Windows windowless spawn) but is what most of the ecosystem, including context7, actually needs |

| 5 | Confused deputy | **Session taint, +1 rung**, sticky, inherited by follow-ups. Network taints on first result; MCP taints on activation because tool descriptions are third-party prompt text. See §3.1 |
| 6 | Preflight caching | **Asymmetric: cache success briefly, never cache failure.** A 4-follow-up review loop pays one handshake; a fix-then-retry always gets an honest re-probe, which is the moment a stale verdict would be worst |
| 7 | Pending `ask_human` hits the idle reaper | **Expire as a recorded denial, and keep it resumable.** The lane gets `error: approval timed out`, reroutes, and the council finishes degraded with provenance — absence of a human never authorizes spend. The durable checkpoint is retained so a late approval re-runs *that lane* rather than discarding the run |

Decision 7 carries a dependency worth flagging: lane-level replay sits on
top of the per-lane parking already scheduled for M-A. If that proves
expensive, the fallback is the plain form — recorded denial, continue
degraded, no resume — which satisfies the safety property on its own.

---

## 5. Sequencing

M-A gates everything. M-C is the cheap proof. M-B is independent of the
providers and can run in parallel. M-D must not land before M-A. M-E/M-F
last, because they are what make the confused-deputy invariant load-bearing.

```
M-A ──┬── M-C ──┬── M-D ──┬── M-E ── M-F ── M-G
      └── M-B ──┘         └── (confused-deputy invariant)
```
