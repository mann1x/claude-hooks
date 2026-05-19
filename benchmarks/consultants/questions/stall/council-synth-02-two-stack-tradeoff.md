---
id: council-synth-02-two-stack-tradeoff
tier: council
source: synthetic (audit-style)
task: |
  Compare two production LLM-gateway stacks (LiteLLM-proxy vs
  Custom-FastAPI-with-Ollama-Pro) across five axes — latency,
  reliability, observability, ops cost, and feature velocity —
  and recommend one for a team of 3 engineers serving 12 internal
  customers with mixed model usage. Show your reasoning for each
  axis; the recommendation must follow from the axis scores.
notes: |
  Audit-style architectural tradeoff. Council under load produces
  multi-round researcher analyses + critic challenges. Council
  effort = "medium".
---

You are advising a 3-engineer team on which LLM-gateway stack to
build their production architecture on. They serve 12 internal
customers (different teams in the same org) with mixed model usage:
~60% requests go to Ollama-Pro cloud models (kimi, glm, gemma4),
~40% to a self-hosted llama.cpp pool for sensitive prompts. Daily
volume ~250k requests; p99 latency target 30s; uptime target
99.5% (down to 99.0% is tolerable during incidents).

**Stack A — LiteLLM Proxy**
- Mature open-source gateway, ~3-year-old project
- Supports 50+ upstream providers out of the box
- Built-in rate limiting, retry, model fallback chains
- OpenAI-compatible API surface
- Observability via Prometheus + Langfuse integration
- Operational burden: docker container, postgres for state, one
  config YAML
- Feature velocity: weekly releases, large community, frequent
  breaking changes on minor versions

**Stack B — Custom FastAPI + Ollama-Pro upstream**
- Built in-house, 6 weeks of dev time
- Single upstream (Ollama-Pro), no provider abstraction
- Hand-rolled rate limit, retry, fallback (limited to 2-step chains)
- OpenAI-compatible surface only for the chat endpoint
- Observability via SQLite stats DB + custom dashboard
- Operational burden: one Python process, one SQLite file
- Feature velocity: team owns the codebase; ship features in
  hours, but every new provider is a fork

Compare on five axes. For each, give one paragraph of analysis
and a 1-5 score for each stack (5 = best). Axes:

1. **Latency** — p50 + p99 wall under realistic load.
2. **Reliability** — uptime under upstream failures, blast radius
   of bugs, rollback safety.
3. **Observability** — what does an on-call engineer have when an
   incident hits at 3 AM?
4. **Ops cost** — engineer-hours per month for routine
   maintenance + upgrades.
5. **Feature velocity** — how fast can the team ship a new
   feature requested by an internal customer?

Sum the scores. The stack with the higher sum is your
recommendation. Justify the final recommendation in one
paragraph that ties the axis scores back to the team's
constraints (3 engineers, 12 customers, mixed model, 99.5%
uptime target).
