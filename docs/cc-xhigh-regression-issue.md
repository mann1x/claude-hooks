# `xhigh` effort quality regression on Opus 4.7 — recommend `medium` until investigated

### Preflight Checklist

- [x] I have searched existing issues; closest priors are #53234 (latency) and #54426 (silent model self-downgrade) — both adjacent but distinct from this report (latency is unaffected here, and our proxy never observed `model_delivered` deviating from `claude-opus-4-7`).
- [x] Single-bug report.
- [x] Latest Claude Code (2.1.121.cc5 → 2.1.126.a4b through the affected window).

### What's wrong

`xhigh` effort on Opus 4.7 has a clear, measurable quality regression starting on the morning of **2026-05-01 UTC** (~00:14 onward). The same prompts on `medium` effort, in the same session and on the same model name, behave normally. Switching the active session to `medium` cleared the symptoms immediately.

User-observed symptoms during the affected window (all on `xhigh`):

- Erratic behavior and trivial mistakes
- Wrong file paths (referencing files that don't exist or live elsewhere)
- Scripts re-run without the same arguments the user had supplied moments earlier
- Hallucinations
- **Post-compaction artifacts** — old or unrelated session messages picked up again as if they had just been sent, after the context-management compaction step

All of these went away on switching to `medium`. The user never went back to `xhigh` after 07:37 UTC; everything observed since has been clean.

### Quantitative evidence — proxy logs + dashboard

Independent verification via [claude-hooks](https://github.com/mann1x/claude-hooks)' transparent local proxy in front of `api.anthropic.com`. The proxy records request metadata (`effort`, `model_requested`, `model_delivered`, `service_tier`, `beta_features`, …) and runs a stop-phrase scanner against responses (the [stellaraccident #42796 canary](https://github.com/anthropics/claude-code/issues/42796) phrases — *ownership-dodging*, *permission-seeking*, *premature-stopping*, etc.). Counts are stored in SQLite and surfaced on a [dashboard](https://github.com/mann1x/claude-hooks/blob/main/docs/proxy.md).

The new `stop-phrases × effort × date` panel pinned the regression cleanly:

![dashboard sp-effort panel](https://raw.githubusercontent.com/mann1x/claude-hooks/main/docs/images/sp-effort-xhigh-regression-2026-05-01.png)

Numeric summary of the `bb75d197` long-running session (the conversation in which the user noticed the regression):

| date       | effort | reqs | ownership-dodging /1k | permission-seeking /1k |
|------------|--------|-----:|----------------------:|-----------------------:|
| 2026-04-28 | xhigh  | 1130 | 6.19                  | 2.65                   |
| 2026-04-29 | xhigh  | 1160 | 0.86                  | 0                      |
| 2026-04-30 | xhigh  | 1338 | **0**                 | **5.98** ← rising      |
| 2026-05-01 | xhigh  |  162 | **55.56** ← 6× spike  | 6.17                   |
| 2026-05-01 | medium |   36 | **0**                 | **0**                  |

Aggregate (all sessions on this account):

| date       | effort | reqs | ownership-dodging /1k |
|------------|--------|-----:|----------------------:|
| 2026-04-28 | xhigh  | 2316 | 6.04                  |
| 2026-04-29 | xhigh  | 2640 | 5.30                  |
| 2026-04-30 | xhigh  | 3050 | 4.59                  |
| 2026-05-01 | xhigh  |  309 | **29.13** ← spike     |
| 2026-05-01 | medium |  486 | **2.06**              |

The `xhigh` rate was **6× higher than the prior 4-day baseline** within hours, while `medium` on the same account stayed clean. The `claude-hooks` `stop_guard` / `sp_*` counters are what surfaced this — without them, the regression would have read as "Claude is being weird today".

### What's *not* the cause (ruled out via proxy data)

Same proxy DB, last 5 days for context:

| signal              | Apr 28 | Apr 29 | Apr 30 | May 1 |
|---------------------|:------:|:------:|:------:|:-----:|
| `model_delivered`   | opus-4-7 | opus-4-7 | opus-4-7 | opus-4-7 |
| `model_divergence_count` | 0 | 0 | 0 | 0 |
| `service_tier`      | standard | standard | standard | standard |
| `beta_features` (10 flags) | identical | identical | identical | identical |
| 429s                | 0 | 0 | 0 | 0 |
| Throughput          | 51 tok/s | 49 | 53 | 51 |
| Cache hit rate      | 98% | 98% | 98% | 98% |

So this is **not**:

- Silent model substitution (cf. #54426 — that report's symptom does not reproduce here; `model_delivered` stayed `claude-opus-4-7` throughout).
- Service-tier rerouting.
- A new beta flag rolling out.
- Throttling / 429s.
- A throughput regression (the latency profile in #53234 is also not what's happening here).
- A `cc_version` issue (the affected window spans `2.1.121.b14`, `2.1.121.cc5`, `2.1.123.0f2`, `2.1.123.ac1`).

It looks like a quality-only change to whatever `xhigh` effort routes to internally — most likely an inference-side rollout that affects the `xhigh` thinking budget / serving path differently from `medium`.

### Steps to reproduce

1. Open a long-running Claude Code session (~200+ messages, post-compaction) with `/effort xhigh` on Opus 4.7.
2. Run normal coding work that touches multiple files.
3. Watch for:
   - Tool calls referencing files that don't exist at the path Claude wrote
   - Scripts being re-invoked with arguments different from the ones you specified two turns ago
   - Sentences reappearing in the assistant's output that match prior, unrelated turns
4. Switch to `/effort medium`. Same session, same prompts → behavior normalizes.

### Workaround

**Use `/effort medium` until this is investigated.** Across this account's data:

- `medium` consumes a fraction of the output tokens of `xhigh` (today's medium avg = 620 output tok vs xhigh = 806).
- `medium` shows zero ownership-dodging / permission-seeking incidents over 486 measured requests today.
- Subjective quality (per the user who experienced both): noticeably better on `medium`, no compaction-artifact symptoms.

**Verdict from this account: `medium` is both cheaper and better right now.** `xhigh` is currently a regression, not an upgrade.

### Why I'm filing this even though Anthropic doesn't seem to read issues

To recommend other users stick to `medium` for the time being. The regression is invisible from the inside ("Claude is just being weird") and easy to misattribute to one's own prompt or codebase; having a public report with quantitative evidence helps others avoid burning a day chasing their own tail.

If anyone from the inference team *does* read this: the `claude-hooks` stop_guard data is reproducible — happy to share the SQLite file privately if it would help isolate the rollout window.

### Environment

- Claude Code 2.1.121.cc5 → 2.1.126.a4b (regression spans both)
- Opus 4.7 (1M context), `effort: xhigh` → `medium`
- Linux (Debian 11) primary, Windows 10 secondary (both affected)
- Plan: Claude Max
- Account behind the [claude-hooks](https://github.com/mann1x/claude-hooks) transparent proxy
- Stop-phrase detection: [@stellaraccident's #42796 canary list](https://github.com/anthropics/claude-code/issues/42796)

### Related

- #53234 — Opus 4.7 (1M context) latency regression Apr 24+ (different axis: latency vs quality)
- #54426 — Opus 4.7 1M xhigh silent self-downgrade to Sonnet (different mechanism: our `model_delivered` never deviated)
- #50623 — Opus 4.7 token consumption / performance degradation Apr 19+
- #46838 — Claude Max thinking-budget / effort-persistence broken
