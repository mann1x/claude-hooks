# tool_executor on/off A/B — 2026-05-18

Controlled A/B of the same M14 store-reaper question against the
`xhigh` topology with `tool_executor.enabled=True` vs `=False`. Both
runs hit the same daemon (PID stable, commits `e3bf437` + prior at
HEAD), same models, same configuration except the one bit.

User asked for this comparison after a multi-run sweep proved the
fabrication problem was glm-5.1 in researcher role, not the
synthesizer; the natural follow-up was "what is the role actually
buying us?"

## The question

> In the M14 store reaper at `consultants/engine/store_reaper.py`,
> the critical invariant is that research originals only get
> deleted after a successful distillation write to the project
> namespace. Walk me through the exact code path that enforces this,
> and identify any edge case where it could fail.

Submitted to `claude-consultants consult --effort xhigh --cwd
/srv/dev-disk-by-label-opt/dev/claude-hooks` via the daemon's
`/v1/consult` HTTP endpoint.

## Configuration

Both runs:

- Topology: `council`
- Effort: `xhigh`
- Models:
  - planner / researcher / critic: `gemini-3-flash-preview:cloud`
    (primary). xhigh fans researcher across `+gemma4:31b-cloud` +
    `+glm-5.1:cloud` extras.
  - tool_executor: `gemma4:31b-cloud` (when enabled)
  - synthesizer: `gemma4:31b-cloud`
- Store: enabled, sqlite_vec backend, TTL + distillation on (M14
  defaults).
- CitationLinter: wired at both researcher and synthesizer
  boundaries (#204), text-at-cited-line symbol-match rule (#205),
  source-listing-fabrication guard in researcher prompt (#207).

The one variable: `tool_executor.enabled = True` vs `False`.
Toggled via:

```
claude-consultants config set-role tool_executor --enabled false \
  --project --cwd /srv/dev-disk-by-label-opt/dev/claude-hooks
```

The flag writes to `.claude-hooks/consultants.toml`, picked up on
the next `/v1/consult` request without a daemon restart.

## Numbers

|                           | WITH (csl-1527-9531) | WITHOUT (csl-1554-c8dc) | Δ                |
|---------------------------|---------------------:|------------------------:|-----------------:|
| Wall time                 |          1121 s (18.7 min) |            403 s (6.7 min) | **−64% (≈3×)** |
| Total LLM calls           |                  142 |                      55 |             −61% |
| Prompt tokens             |              2.65 M  |                  1.50 M |             −43% |
| Completion tokens         |                73 k  |                    38 k |             −48% |
| Total tokens              |              2.72 M  |                  1.54 M |             −43% |
| Synth prompt tokens       |               22 728 |                   9 613 |             −58% |
| Synth completion          |                1 329 |                   1 395 |              +5% |
| Researcher LLM calls      |                   46 |                      52 |             +13% |
| Tool_executor LLM calls   |                   93 |                       0 |             −93  |
| Answer chars              |                2 499 |                   2 661 |              +6% |
| Linter annotations in answer |                    0 |                       1 (line drift) |    +1 |
| Edge cases identified     |                    4 |                       5 |              +1  |
| Researcher-side lint hits |                   11 |                       2 |             −82% |

The synth prompt-token delta is the architectural tell. With
tool_executor in the loop, peer findings get re-serialized through
the fanback barrier and aggregated into the synth's prompt — so
the synthesizer sees ~2× more text. Without it, each researcher
lane reasons end-to-end on its own raw tool output; aggregation
shrinks because there's less material to merge.

## Quality analysis

Both answers cover the same enforcement chain — distillation →
write → delete, with the try/except DistillationFailed gating
deletion. Equally correct on the core path, both lint clean on
fresh re-check.

The WITHOUT answer is **substantively more thorough**:

- **Upstream context the WITH answer skips**: traces
  `expire_before` (line 260) and `_group_by_sid_and_kind` (line
  278) explicitly before the gated path. WITH jumps straight to
  the gate.
- **Two extra edge cases**:
  - *Disabled Distillation* (`store_reaper.py:321-327`) — when
    distillation is config-disabled or `self._store is None`,
    research rows route to an `else` branch and get deleted
    unconditionally.
  - *Metadata Corruption* (`store_reaper.py:82-102`) — rows with
    missing metadata or unrecognized namespaces classify as
    `KIND_UNKNOWN` and get deleted unconditionally.

  WITH did NOT identify these; only the WITHOUT lanes did.
- **Sharper on silent-write-failure**: WITHOUT specifically names
  the bare `except Exception` + log warning pattern in
  `ProviderBackedStore._do_put`. WITH calls it abstractly.

The WITHOUT downsides:

- **Truncated mid-bullet** at `**Post...` — the model stopped
  emitting at 1395 completion tokens (well under any cap; gemma
  just stopped under the tightened prompt's hesitation). One
  bullet lost.
- **One linter annotation**: `[no write_distilled_summary at
  this line; line is in sweep_once]` — line drift, not a
  fabrication. The user sees the verdict inline.

## The architectural read

tool_executor was added for x-tier *proper composition* (#103,
2026-05-17) and validated on the M11c-2 tool-executor bench
where it won 87.5% / 5.00 on tool-heavy reasoning questions.
This A/B does NOT contradict that bench — it's a different
question shape.

What's happening on this question:

1. **Fanback barrier serializes peer state**. Each researcher
   lane emits PLAN → tool_executor lanes run tools → researcher
   re-enters REPORT mode. The re-entry sees its lane's tool
   results as serialized text. Information that was implicit in
   the model's working memory during PLAN gets lost on the round
   trip.
2. **Peer findings aggregate before synth**. With tool_executor,
   M11c-3's fanback collects N×M lane outputs and merges them
   for the synth prompt. Two extras (gemma + glm) means three
   versions of each plan item, and the merge is text-level. The
   synth's 22 k prompt tokens are mostly redundant restatement.
3. **Without tool_executor, each researcher lane is a complete
   reasoning unit**. The grep result and its interpretation live
   in the same model context. The synth sees N parallel reports
   instead of N×M, and each is end-to-end coherent.

For a question like "walk me through this code path" — which is
mostly grep + interpret — the extra round trip costs more than
it buys. For a question like "find the bug in this 5-file
distributed call chain" — where tool work is heavy and the
researcher might saturate its context — the specialist role is
likely to win.

## Verdict and follow-up

On this M14-shape grep-light question:

- tool_executor cost **+12 minutes + 1.2 M tokens**.
- Bought **fewer edge cases identified, slightly less detail
  per case**.

That is net-negative.

The user already had this conclusion from experience; this A/B
confirms with numbers. Action items:

1. Flip `tool_executor.enabled` default to `False` (see #211).
2. Document the role's actual cost/benefit profile in
   `docs/consultants-v2.md` — when to enable, when to leave off.
3. **Do NOT** delete the M11c-2 bench result — tool_executor
   still wins on its bench's question shape. Default-off is a
   knob change, not a deprecation.

## Verification artifacts

- WITH: `/srv/dev-disk-by-label-opt/dev/claude-hooks/.claude-hooks/consultants/csl-2026-05-18-1527-9531/`
- WITHOUT: `/srv/dev-disk-by-label-opt/dev/claude-hooks/.claude-hooks/consultants/csl-2026-05-18-1554-c8dc/`
- Daemon log filter: `grep csl-2026-05-18-15{27,54} /root/.claude/claude-hooks-consultants.log`
- Commits at HEAD when these ran: `e3bf437` (research-side
  linter + #207 prompt guard) + `f0e01cb` (#205 symbol-match
  fix) + prior.
