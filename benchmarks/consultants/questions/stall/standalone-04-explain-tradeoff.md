---
id: standalone-04-explain-tradeoff
tier: standalone
source: synthetic
task: |
  Explain the tradeoff between **single-call hard timeout** and
  **per-token cadence watchdog** for upstream LLM monitoring.
  Cover three operating regimes (small responses, large
  responses, mid-stream stall) and give a concrete numerical
  example for each. End with a heuristic rule for picking
  between the two given a target tail-latency SLO.
notes: |
  Multi-step technical explanation. Expected ~700-1100 tokens.
  Tests structured-reasoning cadence with worked numerical
  examples — the kind of prompt that produces long, even
  generation.
---

Two timeout strategies are common in LLM gateways:

- **Single-call hard timeout** — one `timeout=T_max` per request.
  Simple to reason about; cheap to implement; the same number
  gates every response shape.
- **Per-token cadence watchdog** — `stall_s` and `hard_cap_s` as
  two separate thresholds. The watchdog fires when consecutive
  tokens are more than `stall_s` apart, and the absolute ceiling
  cuts off any call beyond `hard_cap_s`.

Walk through three operating regimes, with concrete numbers:

**Regime 1: Small response (~100 tokens, ~5s wall, ~50ms per token).**
- Hard timeout fires at T_max — pick a value that comfortably
  covers 5s. What happens when T_max=3s? When T_max=30s?
- Cadence watchdog: 50ms inter-token gaps stay far below any
  reasonable stall_s. What happens at stall_s=2s? At stall_s=200ms?

**Regime 2: Large response (~2000 tokens, ~120s wall, ~60ms per token).**
- Hard timeout: how do you pick T_max if you don't know whether
  the response will be 100 or 2000 tokens?
- Cadence watchdog: stall_s=5s never fires. hard_cap_s=180s
  succeeds; hard_cap_s=60s cuts off mid-stream.

**Regime 3: Mid-stream stall (~80 tokens emitted then a 60s pause).**
- Hard timeout T_max=180s: the call eventually returns truncated
  output OR succeeds late (depends on upstream). Either way you
  paid for the wait.
- Cadence watchdog stall_s=10s: the watchdog kills the call at
  ~90s wall, returns "stalled" to the orchestrator, which may
  retry.

For each regime, give one paragraph + the numbers above. Then
finish with a heuristic rule: given a target tail-latency SLO
(say, p99 < 30s wall), how do you pick between the two strategies?
What single number does each approach require, and what's the
relationship between them?
