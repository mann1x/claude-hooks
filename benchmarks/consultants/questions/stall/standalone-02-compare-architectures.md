---
id: standalone-02-compare-architectures
tier: standalone
source: synthetic
task: |
  Compare two stall-detection strategies for an LLM gateway:
  (A) global wall-clock timeout per request, (B) per-token
  cadence watchdog with a separate hard cap. Cover correctness,
  failure modes under proxy back-pressure, and operator
  observability. Conclude with a single-paragraph recommendation
  for a multi-tenant gateway serving 50+ concurrent streams.
notes: |
  Architectural compare-and-contrast. Tests structured-reasoning
  cadence — section headers, bullet lists, conclusion. Expected
  ~700-1000 tokens.
---

Two designs are on the table for the gateway's stall-detection
layer:

**Design A — Global wall-clock timeout**

Every chat request gets a single `timeout=N` seconds. If the
upstream doesn't return a complete response within N seconds, the
gateway tears down the connection and returns a 504 to the
caller. Simple; one configuration knob; the same number gates
every model.

**Design B — Per-token cadence watchdog**

The gateway opens a streaming connection and starts a watchdog
thread that fires every `check_interval` seconds. On each tick
the watchdog reads two values from the cadence counter: total
elapsed time and time-since-last-token. If
`time-since-last-token > stall_threshold`, the gateway cancels
the in-flight call cooperatively and retries with a fresh
controller. A separate `hard_cap` covers the absolute ceiling.

Walk through each design on:

1. **Correctness** under happy-path streaming.
2. **Failure mode** when the upstream stalls mid-stream (a
   half-emitted response, TCP open).
3. **Failure mode** when the upstream is slow but progressing
   (large output, legitimately long generation).
4. **Observability** — what does the operator see in metrics +
   logs when each design fires?
5. **Operational cost** — config surface area, per-request
   overhead, debugging difficulty.

Conclude with a one-paragraph recommendation. Which design
should ship in a multi-tenant gateway serving 50+ concurrent
streams? Justify against the items above.
