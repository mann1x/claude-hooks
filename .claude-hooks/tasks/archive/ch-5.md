---
id: ch-5
title: "Council A — providers, capability probe, Ollama account, calibration, timings"
status: done
priority: H
area: consultants.providers
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:02Z
updated: 2026-10-15T22:18:16Z
sessions: [0a7a5bf4]
---

## Description
Milestone A of docs/PLAN-council-cerebriline.md: ProviderCaps probe per (base_url, model) for Ollama/cloud/xOllama/opencoti/llamafile; transport hardening (Retry-After, empty retry, fault classes, learned ceiling, model-missing); /api/me + model-recommendations; full calibration (3 guards + anchored estimate, keyed per lane+model, fix token_calib.py:299-315); per-call timings + realized sampler. Needs xOllama/opencoti test endpoints from the owner.

## Acceptance
- [x] every backend type answers the probe against a live endpoint
- [x] parity cohort: unchanged requests for the current cloud config
- [x] calibration guard tests port the fork's cases

## Log
- 2026-10-15 22:18Z pending → done: Acceptance checked 2026-10-16. (1) Live probe, every backend type: local Ollama (pandorum :11434 stock 0.40.4, backend=ollama, num_ctx 32768, caps, temperature), xOllama (solidpc :11433), Ollama cloud through xOllama (glm-5.3-flash:cloud: window 1048576, max output, thinking values, plan) and through stock Ollama (cloud flag), opencoti (bs2 :8240: backend=opencoti, 52 features, session_ctx_max/kv_pool 2097152, 4 slots, boot_id, DCA), llama.cpp family via llamafile :38092 (backend=llamacpp, n_ctx 16384, 3 slots). (2) Parity: TestMilestoneAWireParity — /api/chat body plain + streamed byte-identical to 2ad9a8e (pre-A) for a :cloud tag on stock Ollama; mutation-checked. (3) tests/test_token_calib_guards.py ports the fork's tokens.test.ts (d2e3297). Milestone A closed.
- 2026-10-15 22:13Z 2026-10-16: host default URLs done (16a9c54). claude_hooks/ollama_upstream.py resolves the Ollama server for consultants engine/CLI, get-advice, caliber proxy, caliber-smart: CALIBER_GROUNDING_UPSTREAM > URL-shaped OLLAMA_HOST > config ollama.url > http://localhost:11434. No LAN address left in shipped code (guard test). solidpc + pandorum config/claude-hooks.json got ollama.url = http://192.168.178.2:11433 (backups *.bak-20261016), so a deploy changes nothing on either. All Milestone A work items done; acceptance boxes not yet ticked (needs a check of each against the record).
- 2026-10-15 21:29Z 2026-10-15: xOllama live probe done on solidpc :11433 (0.40.3-xollama.2). ollama_caps: backend=xollama, 13 /api/xollama features, Modelfile num_ctx (nimble 8194, tev1 2050), trained ctx, capabilities, cloud flag + remote_host from /api/tags, unknown model -> no show fields; loaded_context from /api/ps (8192). Cloud recommendations cover only 6 featured models, so e.g. nemotron-3-nano:30b-cloud has no window/max-output: correct 'unknown', not a bug. X-Context-Window read on plain + streamed (per-request settlement, never kept as a grant without stated num_ctx). session_id / placement / client_pools / /api/engine are plan line 536 = Milestone F (ch-10), not A. Left for A: host-tailored default URLs (plan §8).
- 2026-10-14 14:31Z [0a7a5bf4] Done so far: A1-A3+A5 (7c0f25a, 2f0a83c), A4 calibration (d2e3297), opencoti/llamacpp named endpoints (9ed5d1a, live on bs2 :8240). Left: xOllama live probe (needs an endpoint); host-tailored default URLs (plan §8).
- 2026-10-14 13:22Z [0a7a5bf4] created
