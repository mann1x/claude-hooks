---
id: standalone-01-research-synth
tier: standalone
source: synthetic
task: |
  You are reviewing three short summaries on the same topic.
  Synthesize them into a single coherent answer (~600-800 words),
  flag contradictions explicitly, and end with a one-paragraph
  recommendation backed by evidence from at least two of the
  three sources.
notes: |
  Researcher-style synthesis prompt. Expected response length
  ~600-1000 tokens; tests sustained-generation cadence without
  hard reasoning constraints.
---

Source A (2024 industry blog): "WebSockets are the right primitive
for streaming LLM tokens to the browser because they're bidirectional
and the JavaScript ecosystem has mature client libraries. The main
downside is proxy hostility — some corporate proxies strip WS
upgrades."

Source B (2025 conference talk transcript): "Server-Sent Events are
strictly better than WebSockets for token streaming. SSE is just
HTTP, so every proxy understands it. Bidirectionality isn't needed
when the client only consumes tokens; client-to-server signals
(cancel, mid-flight inject) ride on a separate POST."

Source C (2024 standards body draft): "HTTP/2 and HTTP/3 server push
make both SSE and WebSockets partially obsolete for streaming use
cases. New designs should target HTTP/2-native streaming primitives
where possible; SSE remains the pragmatic choice for HTTP/1.1
back-compatibility."

Synthesize these three positions. Where do they agree? Where do
they contradict? Recommend one transport for a new LLM gateway
deployed in 2026, justifying your choice against the evidence
above.
