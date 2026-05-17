---
id: council-synth-01-proxy-log-rca
tier: council
source: synthetic (audit-style)
task: |
  Root-cause analysis: an LLM gateway's tail latency p99 jumped
  from 8s to 47s overnight. The log excerpts below cover the
  hour around the spike. Identify the root cause, explain how
  each symptom maps to it, and recommend a fix that addresses
  the cause (not just the symptom).
notes: |
  Audit-style multi-step technical analysis modelled on the
  2026-05-15 claude-hooks pathology. The council under load
  produces a multi-round researcher + critic + synthesizer
  trace — exactly the workload shape where the original stall
  pathology surfaced. Council effort = "medium" (avoid xmedium
  multi-model fanout).
---

You are reviewing an incident in a multi-tenant LLM gateway.
Overnight, the gateway's tail latency p99 jumped from 8s to 47s
while p50 stayed at 1.2s. Throughput dropped from 180 req/s
to 65 req/s. No code deploys in the previous 24h.

Log excerpts (timestamps in UTC):

```
02:03:12  INFO   ratelimit_state: upstream "kimi-k2.6:cloud" returned 429 (5/min budget exhausted)
02:03:12  INFO   forwarder: retrying after 12s backoff (attempt 1/3)
02:03:24  INFO   forwarder: retry succeeded (kimi-k2.6:cloud)
02:14:08  INFO   ratelimit_state: upstream "kimi-k2.6:cloud" returned 429 (5/min budget exhausted)
02:14:08  INFO   forwarder: retrying after 12s backoff (attempt 1/3)
02:14:20  WARN   forwarder: retry returned 429 again (attempt 2/3) — escalating backoff
02:14:44  WARN   forwarder: retry returned 429 again (attempt 3/3) — escalating backoff
02:15:32  ERROR  forwarder: max retries exhausted; returning 504 to caller
02:15:32  INFO   stats_db: p99_latency_60s=14.2s (was 8.1s)
[...continuing pattern at 02:17, 02:18, 02:19, 02:23, 02:25, 02:29...]
02:31:04  INFO   stats_db: p99_latency_60s=23.5s
02:45:22  INFO   stats_db: p99_latency_60s=38.1s
02:58:09  INFO   stats_db: p99_latency_60s=47.0s   <-- current
```

Caller-side metric: every 5xx triggers an automatic retry from
the LangGraph runner with the SAME model. Per-model concurrent
streams: kimi-k2.6=18, glm-5.1=11, gemma4=4. Active model =
kimi-k2.6.

Identify the root cause. Walk through how each symptom (rising
p99, dropping throughput, 429s on retry) maps to it. Recommend
a fix that addresses the underlying cause — not just one that
masks the symptom by raising a timeout. Be specific about
config / code changes.
