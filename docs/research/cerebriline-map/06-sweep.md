# 06 — Full-history sweep of Cerebriline (cline fork) for the consultants council

Scope: `/shared/dev/cline` `main`, `git log --since=2026-08-12` = **1,226 commits**
(HEAD `400b75535`, 2026-10-10, build 4.100.246). Read-only sweep. Other slices
cover in depth: (a) providers/budgets/thinking/calibration/Ollama account,
(b) compaction/context, (c) PolyKV, (d) sandbox/shell/tools, (e)
agents/teams/swarm/escalation/atomic. This file covers everything else, plus a
safety-net pass over the whole list.

Sources read besides the commits: `STATE_SUMMARY.md` (stale: last updated
2026-09-25), `CLAUDE.md`, `PLANS.md` (§7d, §7e, §7p in full; headings
otherwise), `CHANGELOG.md` top (upstream 4.1.15–4.1.17), `release-notes/4.100.246.md`,
`prompt-reviews/README.md`, `harness/README.md`, `evals/analysis/`.

Cerebriline paths are relative to `/shared/dev/cline`. Ours are relative to the
claude-hooks repo root.

---

## 1. Area index of non-trivial commits

I classified the commits by subject keyword
(`scratchpad/sweep06/classify.py`), then corrected by hand where I read the
commit. The counts are approximate (±10%) because many subjects span two areas.
Skipped: **213** release, version-bump and merge commits, and **139**
desktop/webview/UI-styling/branding commits.

| area | ≈count | key commits (one line each) |
|---|---|---|
| agents / teams / swarm / escalation *(slice e)* | 171 | `8c26d9d11` struggle supervisor on every delegated agent · `c8ef7aff4` worker struggle supervisor (nudge once, stop only on thinking-budget loop) · `4c3aa0ec7` a thinking-only turn is asked again · `6b7a21ca2` tell the model which argument its rejected call swallowed · `9078bd9f2`/`6222ce903`/`8be3f4f1f` per-spawn seed/temperature, "random" held to ±10% · `c498e9cf4` Jev confidence scorer · `539ffc450` agents_status · `659837c80` graceful stops with a report |
| providers / llms / budgets / thinking / tokens *(slice a)* | 111 | `f45abdd16` reasoning replay decided from a measured capability · `8baed60ce` inline prior thinking when template drops it · `c61cb3600`/`2403f40e8` token counts that cannot be measurements are refused · `178c27320` output cap 96k · `3c1d289c0` llama.cpp/opencoti sent no sampler · `a99c86b8e` unparsed tool call ≠ no tool call · `0ca7a8475` mis-sliced tool name recovered |
| atomic / change protocol / revisions / check_file | 70 | **Excluded by owner** (2026-10-10 ruling: the atomic-task protocol is early-phase and not working properly; it is not a source for inheritance). Not analysed. |
| tools: editor / grep / sed / awk / read / ask_question *(d, partly mine)* | 64 | `44a186d79` an unchanged re-read is a pointer, not a copy · `41059765c` read limit with a measured "read this many lines" · `84ac149b1` grep context 20→50 (char cap already guards) · `66a9dffbc`/`a1ed21493`/`d40489868` grep/awk/sed in-process · `1a1ed15bf` list sent as text, one-reading coercion · `7be796e68` take the shapes models send · `2b54b712d` relative paths against the workspace, not process cwd · `a2fb1d15c` (upstream) search must not OOM on giant one-line files |
| ci / tests / e2e | 53 | `d7d13bc6e` 31 POSIX-only tests · `5c34fa2cf` recorded cassettes one turn short after nudge change · `f67ec80e0` log-sink test reads lines not file |
| compaction / context *(slice b)* | 48 | `4fee8b6bb` compaction as a continuation · `8de6864e2` keep saved compaction when transcript is reshaped · `247e72ce3` compact when output cap goes thin |
| hub / sdk / core infra | 47 | `6859d00e5` stop shipping full transcripts in broadcast events · `83ff8fd2f` cap hub-events DB size (64 MiB + VACUUM) · `eef7958ca` truthful session status after queue-drained turns · `c64743eb3`/`52d5e1a51` parent abort reaches sub-agents and teammates · `6389da4da` hub serves before its discovery record is visible |
| memory / retrieval / library / code index | 37 | `9f42ee459` auto-recall per message with grounded HyDE (port of our docs/hyde.md) · `bc7cedbbd` remember/recall/forget tools · `a860dc15e` memories per workspace · `d6a7d0b95` embed+rerank client · `ce9088226` LanceDB vectors + hybrid RRF · `5db26e633` embedding-model change visible and runnable · `87a23b638` `search_codebase` semantic mode · `5c0e42154` semantic mode for delegated agents · `654a127f7` CLI `--code-index` |
| settings / profiles / config | 32 | `693e15c59` tool-result cap per configuration (profile → provider → global) · `760eb6c03` temperature has one owner · `f727f2ee0` profiles could not carry/let go of caps · `c24f977be` a boundary saves what a debounced field still holds · `a9f98391a` repeat_last_n −1 rejected · `6fa6da515` per-provider reasoning-replay control |
| polykv / kv *(slice c)* | 27 | `3d819eb9a` owner-full refusal grows and resends after Retry-After · `196452947` place swarm agents by measured holding |
| history / chat / queue / events / storage | 24 | `f30518281` session records what it ran with · `645349a34` error count per conversation · `84b0a4c6e` tags · `9e0358ee5` the runtime's notes are not the user's · `3375d3632`/`9725ec24e` sub-agent events carry agent+parent ids · `592cd3573`/`103e9a6a8` stop re-reading config per call |
| documents / media / vision | 24 | `b209660cc`/`7b36d4796` extract_document (OCR, picture descriptions) · `ff04bdec3` each document read in its own process · `5ecdfd68d` only image-capable models, routed to their own host · `cdca56ccf` one endpoint rule for media tools |
| cli | 23 | `ecece6fb8` `--agent-node` · `442d9cabe` `--propose-check auto` (headless has nobody to ask) · `0457757fc` "enabled without a level" is not medium · `3c094427f` `--no-checkpoints` |
| docs | 23 | `1fd8ea559` change protocol and every setting · `19fb62a39` delegated agents, every feature |
| prompts / templates | 20 | `35ebf8f13` a template you wrote outranks a shipped one · `575e351bd` rewrite one tool section, not the whole file · `c434bbfb8` kimi-k3 writes 3 of its own 4 tool sections · `204059f3d` the audit was demanding the rule that caused the loop · `08bc73a6e` (qwen) batch gathering, not edits |
| sandbox *(slice d)* | 19 | `394038d38`/`858cf1cff` private workspaces for every delegated path |
| safety / loop guards / nudges / turn faults | 19 | `823f20dc6` reasoning-channel collapse guard · `eaf80ef5d` re-deriving what it concluded · `c5d421167` thinking at length is not working · `e17c436ab` loop guard stops when the advice runs out · `ff88c806e` identical calls in one message count once · `5fc435627` struggle detector reads the refusal a tool hides · `eaf8ed14f`/`a48e2d2af` turn faults: unparseable stream = transport |
| skills / sdd | 15 | `689be2628` sdd plan in SQLite behind one tool that says which step is due · `9c91bbf19` built-in skills shipped with ext/CLI/SDK |
| mcp | 12 | `10fe8d481` every MCP tool says which server it came from · `ec2e366d3` a server named `vscode` is refused, not shadowed · `d3ad3ae14` borrow VS Code's running MCP servers · `9e7c1a3f9`/CHANGELOG 4.1.17 offline remote MCP no longer stalls startup (10 s connect budget) · `2446030d6` failed OAuth = needs auth |
| telemetry / usage / timings | 9 | `f4bfb8609` per-request output split thinking/text/tool-input · `a3ce3283d`/`e22cdc0ea`/`162292870` engine timings (Ollama/llama.cpp) under each request · `6545b37f0` connection count split by what each spent · `c870116d1` task.completed on every teardown path |
| hooks | 8 | `8fe5a196c` tool-hook contextModification reaches the model · `9b9a067fb` PostToolUse output collected, its control honoured · `56fa1d039` time hook discovery · `cea134be0` a hook spawn failure must not crash core |
| other | 18 | `1a507b630` main is the trunk, harness in-repo · `721e0b7c9`/`5336ba07a` migrate data dir safely · `29530caa5` FTS session search · `ff14ab601` agents create scheduled tasks |

---

## 2. Council-relevant items outside slices a–e

Ordered by expected value to the council. Each item gives the Cerebriline
source, what it does, and how it maps onto our code.

### 2.1 Auto-recall with a relevance gate and per-session dedup (memory-recall)

- **Where:** `sdk/packages/core/src/services/retrieval/memory-recall.ts`
  (`9f42ee459`). The file header cites our `docs/hyde.md` as the design
  source.
- **What it does:**
  - The search order is: raw query; then grounded HyDE when the first search
    found something; then RRF fusion with `RRF_K=60`, both rankings weighted
    equally (`:174-192`). HyDE grounds on the top 3 notes, 1,500 characters
    in total (`:47-48`), with a 20 s timeout (`:50`) and a 200-entry
    expansion cache. When the raw search finds nothing, it grounds on the
    newest notes instead (`:307-312`).
  - **New relative to ours:** `isAboutTheSearch()` (`:142-160`) attaches an
    unasked note only when one of three things holds. A reranker scored it
    (`relevance` set), or cosine is at least 0.5 (`AUTO_RECALL_MIN_SIMILARITY`,
    `:55`), or it shares at least 2 stemmed non-stopword words with the query
    (`:57`, `:119-139`). The point is that "a search always returns its best
    matches, however poor".
  - **New:** a per-session set of note ids already given, so a note is
    injected once per task (`:210-226`, `:338-348`). It is bounded to 200
    sessions.
  - **New:** `clampRecallQuery()` keeps the head and tail of a pasted log,
    up to 2,000 characters (`:163-171`). Queries under 12 characters are
    skipped (`:43`).
  - Every log line tells "expansion found nothing" apart from "never ran"
    (`:313-322`).
- **Maps to:**
  - `consultants/engine/council.py:2101-2126`. The researcher calls
    `recall_research(store, sid, recall_query, limit=5)`
    (`consultants/engine/store.py:791`) and injects whatever comes back as
    `peer_findings`. There is no relevance floor, so a lane with no real
    peers still gets the 5 nearest unrelated findings. Port
    `isAboutTheSearch` (the similarity floor plus the shared-content-word
    test) into `recall_research` / `format_findings_block`
    (`store.py:881`).
  - Per-session "already given" dedup applies to follow-up rounds. Today the
    filter at `council.py:2117-2124` drops only the lane's own findings.
  - Head+tail clamp for `recall_memory` queries
    (`claude_hooks/caliber_proxy/tools.py:368`) and the recall query in
    general.
  - The council has **no automatic memory recall** at planner entry. It
    recalls only when a model calls `recall_memory`. The Cerebriline header
    states the same problem plainly: "small models mostly do not ask". A
    planner-entry auto-recall over the user store, gated as above, is cheap.

### 2.2 Unchanged re-read → pointer, with a compaction refresh valve

- **Where:** `sdk/packages/core/src/extensions/tools/executors/unchanged-reads.ts:1-35`
  (`44a186d79`; opt-in since `5359b8fc7`).
- **What it does:** the "same read" key is path + window + line-numbers flag.
  A repeat whose **content hash** is unchanged gets a pointer. Every
  `REFRESH_EVERY=4` repeats it serves the real content again, because "after
  a compaction the earlier read may have been summarised away".
- **Maps to (real gap):** `claude_hooks/agent_loop/runner.py:331-358`.
  - The dedup key is the raw `name|args_str`, not canonicalized. A dict
    argument is only `sort_keys`-ed in the within-turn collapse at `:530`,
    not here.
  - `seen` (created at `:433`) persists across iterations and is **never
    invalidated** after compaction (`council.py:1074 _plan_and_compact`)
    or after a non-read-only tool runs. A lane that compacts and then
    re-reads gets the stub "you already called X, use that prior result",
    which points at a result that has been elided.
  - Fix: on every Nth repeat, or whenever the payload budgeter elided
    messages, serve the real result again. Key on the content hash for
    `read_file`, as `consultants/server/tool_cache.py` already does with
    size+mtime. Clear `seen` when a non-read-only tool runs, mirroring the
    tool_cache rule.

### 2.3 "No tool call" is not "done": four distinct nudges (prompt/format)

- **Where:** `sdk/packages/shared/src/prompt/format.ts`:
  - `NO_TOOL_CALL_NUDGE_MESSAGE` (`:185`) and `ASK_QUESTION_NUDGE_CLAUSE`
    (`:209`).
  - `NON_CONVERGENCE_NUDGE_PREFIX` / `buildNonConvergenceNudge` (`:259-291`)
    for "thinking at length is not working", `c5d421167`.
  - `UNPARSED_TOOL_CALL_NUDGE_PREFIX` / `unparsedToolCallInText()`
    (`:292-373`) for a `<tool_call>` left in the text that the parser
    rejected, `a99c86b8e`. It counts restarts and names the function.
  - `ANNOUNCED_INTENT_NUDGE_PREFIX` / `announcedIntentWithoutActing()`
    (`:397-426`) quotes the model its own sentence ("Let me propose a check")
    once per run, `e77fa2b22`.
  - Plus `4c3aa0ec7`: a turn that is **thinking-only** is asked again,
    because in 11 of 23 such turns the tool call sat *inside* the reasoning
    channel.
- **Maps to:** `claude_hooks/agent_loop/runner.py:494-513`.
  - Any turn without `tool_calls` ends the loop. The only retry is
    `force_first`, which fires once before the first tool call.
  - A researcher lane that writes "I will now grep for X" after its first
    tool call, or that leaves a raw `<tool_call>` in content or reasoning,
    becomes that lane's final answer. The synthesizer then ingests an
    announcement.
  - Port the announced-intent and unparsed-call detectors as one bounded
    retry, once per run.
  - Note that `stop_guard` (`claude_hooks/stop_guard.py`) already holds
    commitment-stall phrases for Claude Code itself. The pattern list could
    be shared.

### 2.4 Content-based loop guards, beside our cadence-based stall monitor

- **Where:**
  - `sdk/packages/agents/src/reasoning-loop-guard.ts:1-56` (`823f20dc6`).
    It is a streaming guard on the reasoning channel with three signals
    (low-uniqueness, line-cycle, phrase-cycle) and defaults `minChars 4000,
    window 60, maxUniqueInWindow 4, minCycles 20`. Its thresholds were set on
    1,556 reasoning blocks. "Length is never the signal."
  - `reasoning-repetition.ts` (`eaf80ef5d`) handles a paragraph repeated
    verbatim within one completed block. It needs both `maxRepeatTrip 4` and
    `duplicateFractionTrip 0.25`, and it **nudges, never cuts**: 14% of the
    runs it fired on still fixed the task.
  - `sdk/packages/core/src/runtime/safety/loop-detection.ts:198`
    `toolCallSignature` uses canonical sorted-key JSON. `e17c436ab` stops
    once the three pieces of advice have been read. `ff88c806e` counts
    identical calls in one message once.
  - `struggle-detector.ts:34-123, :257 refusalIn`: a tool that returns
    `{success:false}` inside a "successful" result counts as a failure. This
    raised `STRUGGLE_FAILED_CALLS` from 4 to 6 once it was measured.
- **Maps to:**
  - `consultants/engine/stall.py` (header `:1-40`) detects *token-cadence*
    stalls only. A lane that streams a degenerate repetition at full speed
    is "PROGRESSING" until the hard cap.
  - `chat_streamed` (`claude_hooks/get_advice/chat_client.py`) already
    concatenates every reasoning delta, which is the input the guard needs.
    Port `ReasoningLoopGuard` as a `StallController` verdict, e.g. a
    `DEGENERATE_STALL` class, and keep "nudge, don't cut" for the softer
    repetition signal.
  - Our tools return `"error: …"` strings
    (`claude_hooks/caliber_proxy/tools.py:563-590`). A refusal counter in
    the loop's `on_tool` hook could feed the xauto escalator and the
    retrospective (`consultants/engine/retrospective.py`).

### 2.5 Turn-fault taxonomy: transport vs refusal vs model fault

- **Where:** `sdk/packages/shared/src/agents/turn-faults.ts:1-120`
  (`a48e2d2af`, `eaed6b7b2`).
  - `transport` patterns: llama.cpp "server is shutting down", 503
    "loading model", ECONN*, undici `terminated:` and `HTTPParserError`,
    "invalid input batch", the AI-SDK `TypeValidationError`/`JSONParseError`
    on stream chunks.
  - `refusal`: admission or rate limit, waited out with backoff.
  - KV eviction gets its own class.
  - A 400, a bad tool call or an overflow is the model's fault and is
    **not** retried.
  - Ruling: an agent's infrastructure fault is never reported as its answer.
- **Maps to:** `claude_hooks/get_advice/chat_client.py:28, :531-540,
  :704-714` retries 429/5xx/URL errors by HTTP class. Two gaps:
  - A stream that dies mid-body with a parse error.
  - `data: null` keepalive chunks.

  `consultants/engine/events.py:243` lumps everything under `"raised"`. A
  shared classifier would let the stall monitor and
  `_compose_degraded_answer` (`council.py:1658`) tell "server restarted"
  (retry) apart from "model produced garbage" (degrade).

### 2.6 Tool-argument coercion where there is exactly one reading

- **Where:** `sdk/packages/core/src/extensions/tools/schemas.ts`:
  - `LooseBoolean` (`:65`), `parseArrayString` (`:202`),
    `MALFORMED_ARRAY_MESSAGE` (`:399`), `stringToList` (`:414`),
    `listOrSingle` (`:439`). From `1a1ed15bf`: 49 of 69 "array sent as
    string" failures were closed but under-escaped, not truncated.
  - `7be796e68` accepts the shapes models actually send (singular `query`,
    aliases).
  - `0ca7a8475` recovers a tool name that a provider mis-sliced
    (`llms/src/providers/ai-sdk.ts`), only when the arguments parse.
  - `274e0c574` (upstream) refuses to "repair" truncated JSON by inventing a
    string terminator. It once replaced 14,127 bytes with 572.
  - `d243adbc8` logs every call's raw arguments as sent, before parsing,
    keeping head and tail past 32k.
- **Maps to:** `claude_hooks/caliber_proxy/tools.py:576-581`. Here
  `json.loads` either succeeds or the call fails ("tool arguments not valid
  JSON"), with no coercion of `"true"`, of `"[...]"` sent as a string, of
  `start_line` as a string float, or of a singular/plural alias. The recorder
  stores `args` (`consultants/engine/recorder.py` events table), which
  already covers the logging half.

### 2.7 Read and grep tool limits that tell the model *how much*

- **Where:**
  - `41059765c` (`executors/file-read.ts`, `output-limits.ts`): refuse
    past 24k characters, and say "read lines 1-272" computed from the
    rendered output.
  - `84ac149b1`: grep context up to 50 lines, with a streaming
    48,000-character cap and the message "(output stopped at 48000
    characters — narrow the pattern, or pass fewer paths)".
  - `a2fb1d15c`: no unbounded buffering of matched lines.
  - `2b54b712d`: resolve against the workspace, never `process.cwd()`.
  - `67a0141cb`: "an empty answer is not always an answer". An empty
    `workspace_symbols` is not authoritative for script inside `.html`, and
    every answer about a file that does not parse opens by saying so.
  - `daf819f2e`: rename `code_intel` → `ask_lsp`, because the name is what
    models retain.
- **Maps to (bugs):**
  - `claude_hooks/caliber_proxy/tools.py:221-231`. When `read_file`
    truncates at 48 KB, the header still says `path:s-e` for the
    *requested* range. The note says "use a smaller line range" without the
    line the cut stopped at. A researcher can cite `path:e` for lines it
    never saw, which also weakens the CitationLinter's input
    (`consultants/engine/citation_linter.py`). Fix: rewrite the header to the
    last line actually shown and state "next: start_line=N".
  - `grep` (`:296-365`) has no context-lines option.
- **Maps to (design):** the council exposes no LSP or code-graph tool to
  lanes. The "empty is not proof" lesson is our own memory
  "Empty diagnostics prove nothing". Any lane-facing LSP/code_graph tool
  should carry it in its description.

### 2.8 Semantic code search as a lane tool

- **Where:**
  - `sdk/packages/core/src/services/retrieval/code-index.ts:1-38`: a second
    Library per workspace, one document per source file, files over 256 KB
    and lockfiles/min/generated files skipped, capped at 20k files.
  - `extensions/tools/code-search.ts`: refreshes in the background, and a
    search made before the index is ready says so instead of blocking.
  - `definitions.ts:499-560`: `search_codebase` gains `mode:
    "semantic"|"regex"`, offered **only** where the index exists, so nothing
    is offered that fails.
  - `5c0e42154`: delegated agents get it, with lines mapped into the agent's
    copy.
  - `retrieve.ts:1-60`: hybrid search with weighted RRF over BM25+vector
    (`bm25Weight 0.5`, `topK 5`, `topKReranker 3`, a relevance threshold),
    and on overflow the documents are halved up to 3 times before reranking
    is abandoned (`RERANK_SHORTENINGS`, `:28`).
- **Maps to:** the researcher toolset is `survey_project/list_files/read_file/glob/grep/recall_memory`
  (`claude_hooks/tool_registry/builtin.py:27-29`). We already have pieces:
  the pgvector/sqlite_vec stores, the code-graph MCP, and claudemem. A
  `semantic_search` provider in `claude_hooks/tool_registry/` would answer
  "where does X happen" questions that grep cannot. It should be gated the
  same way: offered only when an index exists for the cwd. It must also go
  into `CACHEABLE_TOOLS` (`consultants/server/tool_cache.py`) only if it is
  read-only and the index is stable within a run.

### 2.9 MCP tool provenance and connect budget

- **Where:**
  - `sdk/packages/core/src/extensions/mcp/tools.ts:24-33`: every MCP tool
    description ends `From the "<server>" MCP server.` One clause per tool
    beats a prompt section that would be paid for on every request.
  - `ec2e366d3`: reserved server names are refused, not shadowed.
  - PLANS §7e (`PLANS.md:1131-1214`): the `server__tool` name is
    load-bearing for auto-approve.
  - CHANGELOG 4.1.17: an unreachable remote MCP server gets a 10 s connect
    budget and no longer stalls startup.
- **Maps to:** the council has no MCP surface today. `claude_hooks/tool_registry/`
  has only `builtin` and `git_provider`. If MCP tools are added (e.g. pgvector
  KG, lsp), copy all three rules: provenance clause, reserved-prefix
  refusal, bounded connect at `_submit_run` time.

### 2.10 Per-spawn sampling diversity, held near the model's own

- **Where:** `sdk/packages/core/src/extensions/tools/team/spawn-sampling.ts:54-57,
  :140-145, :291` (`9078bd9f2`, `6222ce903`, `8be3f4f1f`).
  - A seed that covers N agents is offset per agent (seed, seed+1, …).
  - `temperature:"random"` draws within ±2% of the model's own temperature.
  - `temperature_range` is capped at **10%**, because a lead asked for 60%
    and its agents at 0.6–1.6 "did not work".
- **Maps to:** `claude_hooks/model_sampling.py` and the x-tier fan-out
  (memory: "x-tier diversity is killer"). Our lanes diversify by *model*
  only. Two lanes on the same model sample identically unless the provider
  randomises. Offsetting the seed per lane is free diversity. Any
  temperature jitter must stay within ±10% of the template value, the same
  bound the "glm-5.3 at 0.7" measurement supports.

### 2.11 Settings layering conventions

- **Where:**
  - `693e15c59`: resolution is **profile → provider entry → global
    setting**, so nothing configured earlier changes behaviour.
  - `760eb6c03`: every parameter has exactly one owner. Two stores reaching
    one request let assembly order decide.
  - `35ebf8f13` + `sdk/packages/shared/src/prompt/template-types.ts:148-158,
    :255-270`: specificity is `default 0 < provider 1 < family 2 < model 3`.
    Within a rung, the longest literal pattern wins, an exact match beats a
    wildcard of the same length, exclusions veto, and a user template
    outranks a shipped one (`:460`).
  - `sdk/packages/shared/src/retrieval/memory-settings.ts:120-150`: the
    normaliser validates field by field and falls back to the default *per
    field*.
  - STATE_SUMMARY "Adding a setting touches eight places, three of which
    fail silently".
- **Maps to:**
  - `claude_hooks/model_sampling.py:155-162` already uses literal-character
    specificity and the same three layers (`.claude/rules/model-sampling.md`).
    It lacks the exact-beats-wildcard tiebreak and exclusions. It also
    breaks ties on dict order, so two equal-specificity patterns resolve
    nondeterministically across JSON merges (compare memory "Order is not a
    clock").
  - Consultants config already layers user → project → `override_user_global`
    (`consultants/config.py`). The "one owner per knob" check is worth a
    test: temperature can arrive through the model-sampling templates *and*
    a role's own options.

### 2.12 Record what a run ran with; engine timings per call

- **Where:**
  - `apps/vscode/src/sdk/session-settings-snapshot.ts:1-40` (`f30518281`):
    the session's metadata records window, budget, reasoning, sampler and
    tools at start, and again on resume. Credentials and headers are excluded
    by construction.
  - `sdk/packages/llms/src/providers/request-timings.ts:60-110` (`a3ce3283d`,
    `162292870`): reads Ollama `prompt_eval_count/_cached_count/_duration`,
    `eval_*`, `load_duration`. The prefill rate is computed over
    *evaluated* tokens (prompt − cached). The naive rate came out at 1.44M
    tok/s.
  - `f4bfb8609` / `sdk/packages/agents/src/output-split.ts`: output is split
    into thinking, text and tool input per request.
  - `645349a34`: error count per conversation in history.
- **Maps to:**
  - `consultants/engine/recorder.py:41-56`. `meta` holds `models_json` and
    effort but not the resolved sampling/caps/tool list. Per-call
    `request_json` holds the payload, which is the implicit half.
  - Nothing in `claude_hooks/` or `consultants/` reads `eval_duration`,
    `prompt_eval_duration` or `load_duration` (grep finds only
    `prompt_eval_count`). `.claude/rules/consultants-runtime.md` says
    stall thresholds are recalibrated "from `transcript.db` `llm_call`
    rows". Engine timings and the cached count would separate cold load,
    prefill and decode in that calibration.
  - The output split would explain truncation and budget cases in
    `retrospective.py`.

### 2.13 Per-model prompt findings

- **Where:**
  - `prompt-reviews/` + `core/src/extensions/config/prompt-template-review.ts`
    (`575e351bd`, `c434bbfb8`): models rewrite their own tool sections, a
    delta per section, judged by the review module. `204059f3d`: "the audit
    was demanding the rule that caused the loop".
  - `08bc73a6e` (qwen): batching *gathering* is fine, batching *edits* is
    what costs.
  - `a16e35478`: tell the model that its own previous reasoning is not in
    front of it, or it distrusts the notice.
  - `9e0358ee5`: harness notes carry `origin: harness`, never shown as the
    user.
- **Maps to:**
  - Our role prompts (`consultants/engine/council.py:260 _role_prompt`,
    `build_*_messages` at `:682-830`) are model-agnostic. Per-family prompt templates with
    the specificity rule in 2.11 are a cheap A/B for weak lanes.

### 2.14 Spec-driven plan in a database, behind a "what is due" tool

- **Where:** `sdk/packages/core/src/services/sdd/sdd-engine.ts` (1,520
  lines) and `extensions/tools/sdd-tool.ts` (`689be2628`).
  - The plan lives in `.sdd/sdd.db`.
  - Every action checks its turn and is refused with the step that is due.
    A task cannot be completed without its verify output, and a slice cannot
    be closed on a failed acceptance check.
  - `next` returns the one due step.
  - Measured: glm-5.3 built a 3-requirement project in 82 s/37 iterations,
    against 8 m/121 iterations on markdown.
- **Maps to:** the planner's free-text plan, parsed by `parse_plan_items`
  (`council.py:832`), and our task tracker (`claude_hooks/tasks/`). It is a
  pattern for the coder/tool_executor path if it ever runs multi-step
  plans: state lives in a store, and the model only asks "what is due".

### 2.15 Calibrated confidence from an external scorer (Jev)

- **Where:** `sdk/packages/core/src/extensions/tools/jev.ts:37-55` (`c498e9cf4`).
  - Typed questions (yes/no, pick one, score) come back as probabilities.
  - Floors are 0.6 normally and 0.85 for high stakes, with a 15 s timeout.
    A binary answer uses `|2p−1|`.
  - All questions go in one call, measured ~12× cheaper than one call each.
  - It feeds escalation appraisal, bounded at 8 s.
- **Maps to:** `consultants/engine/council.py:102 _extract_confidence` and
  the xauto escalator. Both rely on the model's *self-reported*
  confidence, which the commit calls "the one thing a coding model reports
  badly about itself". An independent scorer, or a small judge model asked
  typed questions in one batch, could gate escalation and the
  accept/follow-up review loop.

### 2.16 Hub/event hygiene that applies to our SSE and transcript.db

- **Where:**
  - `6859d00e5`: events must not embed the transcript (one case reached
    25 GB of RSS).
  - `bcbd0823a`: snapshot copies happen lazily, only for consumers that
    read them. 165k events against a 554k-character conversation.
  - `83ff8fd2f`: the event DB has a size budget (64 MiB), pruned after
    every 16 MiB appended, with VACUUM; `DELETE` alone never shrinks SQLite.
  - `eef7958ca`: status projected from snapshots must be the real status,
    or clients wedge on "running".
  - `9725ec24e`: *every* event, including the runtime's own notices, is
    stamped with agent and parent ids.
- **Maps to:**
  - `consultants/server/events_sse.py` and the `runtime_events` table
    (`recorder.py:86-95`). Check that no payload carries full message
    lists.
  - Per-session `transcript.db` files have no size budget.
  - Lane-originated notices (stall and retry events) need `lane_idx` on
    every row. The column exists; the question is whether every emitter
    sets it.

### 2.17 Library / documents as an optional research source

- **Where:**
  - `extensions/tools/library-tools.ts:200 search_library, :363 list_library`.
  - `services/retrieval/chunker.ts:1-40`: markdown-header-aware, with a
    paragraph → line → sentence → word cut ladder, size 1500 and overlap 100.
  - `ff04bdec3`: each document is read in a child process, so a bad PDF
    cannot take the host down.
  - `04c68e511`: a truncated EPUB is an error, not a ten-minute wait.
- **Maps to:** lower priority. If a council ever researches over PDFs or
  docs, use a `search_library`-style read-only provider in `tool_registry`
  with child-process extraction. The chunker's cut ladder is a better
  splitter than a fixed window, if `distillation.py` ever chunks long
  research.

### 2.18 (removed: headless approval of the atomic protocol's check; atomic is excluded by owner)

### 2.19 Smaller items

- `0457757fc`: `reasoning:{enabled:true}` without a level is not "medium".
  The CLI was converting it to `think:"medium"`, which bounds thinking at
  2k. Check `runner.py:398-407`, where `think` is mapped to
  `reasoning_effort`, for the same defect.
- `c870116d1` (upstream): emit completion telemetry from *every* teardown
  path. Applies to `_settle_inflight` and recorder `finished_at` on
  suspend, fail and kill.
- `8fe5a196c` / `9b9a067fb`: Cline's own hook system now delivers
  `contextModification` and honours PostToolUse control. This is relevant
  to claude-hooks in general, not to the council.
- `5c34fa2cf`: after a runtime behaviour change (the added nudge), recorded
  replay cassettes run one turn short. This is relevant if
  `tests/test_consultants_*` replay fixtures pin turn counts and the
  nudges in 2.3 are added.

---

## 3. Items the other slices may miss

1. **Content-based loop guards.** `reasoning-loop-guard.ts`,
   `reasoning-repetition.ts` and `c5d421167` sit in `sdk/packages/agents`,
   but they are safety guards, not orchestration. Slice (a) may treat them
   as thinking budgets and slice (e) as agents. Neither framing covers
   "streaming degenerate output at full token rate", which our cadence
   stall monitor cannot see (2.4).
2. **Turn-fault classifier** (`shared/src/agents/turn-faults.ts`). It looks
   like agents/swarm resilience, but it is a provider-error taxonomy every
   council lane needs (2.5).
3. **Unchanged-read pointer and compaction refresh** (`44a186d79`). Slice
   (d) may file it under tools and slice (b) under compaction. The
   interaction is the point: our `seen` stub survives compaction (2.2).
4. **The no-tool-call nudge family** in `shared/src/prompt/format.ts`
   (`a99c86b8e`, `e77fa2b22`, `4c3aa0ec7`, `c5d421167`). It is spread
   across "runtime:" and "agents:" subjects with no single area (2.3).
5. **Tool-argument coercion** (`1a1ed15bf`, `7be796e68`, `0ca7a8475`,
   `274e0c574`). It lives in `schemas.ts` / `ai-sdk.ts`, between slice (d)
   tools and slice (a) providers (2.6).
6. **`read_file` truncation honesty and "say how much"** (`41059765c`,
   `84ac149b1`). It ties into our CitationLinter (2.7).
7. **Per-spawn seed offset and ±10% temperature bound** (`8be3f4f1f`). It
   is filed under agents, but the lesson is about x-tier sampling (2.10).
8. **Jev** (`c498e9cf4`, `f535f2035`). Slice (e) may see only the
   escalation hook; the transferable part is calibrated confidence for the
   review loop (2.15).
9. **Engine timings and the cached-prompt prefill rate**
   (`request-timings.ts`). It looks like UI ("under the request"), but it is
   the missing input for stall recalibration (2.12).
10. (removed: atomic-protocol item, excluded by owner)
11. **Settings "one owner per knob"** (`760eb6c03`) and **profile →
    provider → global** (`693e15c59`). These are config conventions, not
    budgets (2.11).
12. **Hub event hygiene** (`6859d00e5`, `bcbd0823a`, `83ff8fd2f`). These
    are upstream commits slices may skip as "Cline upstream", but they apply
    to our SSE and transcript.db (2.16).
13. **Thinking-only turn with the tool call inside reasoning**
    (`4c3aa0ec7`). Slice (a) "thinking" may cover the budget side but not
    the "call written before the channel closed" parse case.
14. **`a16e35478`**: tell the model its prior reasoning is not visible.
    It matters for our capped-thinking notes (`runner.py:144-197`), which
    replace a turn's reasoning with a note.
15. **`5c34fa2cf`**: test cassettes go stale when nudge behaviour changes.
    A test-infrastructure trap.

### Things checked and found not relevant

Media/video/audio/image generation, desktop/Tauri, ClinePass/billing, Windows
signing, marketplace UI, branding, VS Code LM-MCP bridge internals, the
history tags and news feed UI, and the hub upgrade/drain flow (upstream).
