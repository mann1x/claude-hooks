---
id: ch-5
title: "Council A — providers, capability probe, Ollama account, calibration, timings"
status: pending
priority: H
area: consultants.providers
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:02Z
updated: 2026-10-14T14:31:01Z
sessions: [0a7a5bf4]
---

## Description
Milestone A of docs/PLAN-council-cerebriline.md: ProviderCaps probe per (base_url, model) for Ollama/cloud/xOllama/opencoti/llamafile; transport hardening (Retry-After, empty retry, fault classes, learned ceiling, model-missing); /api/me + model-recommendations; full calibration (3 guards + anchored estimate, keyed per lane+model, fix token_calib.py:299-315); per-call timings + realized sampler. Needs xOllama/opencoti test endpoints from the owner.

## Acceptance
- [ ] every backend type answers the probe against a live endpoint
- [ ] parity cohort: unchanged requests for the current cloud config
- [ ] calibration guard tests port the fork's cases

## Log
- 2026-10-14 14:31Z [0a7a5bf4] Done so far: A1-A3+A5 (7c0f25a, 2f0a83c), A4 calibration (d2e3297), opencoti/llamacpp named endpoints (9ed5d1a, live on bs2 :8240). Left: xOllama live probe (needs an endpoint); host-tailored default URLs (plan §8).
- 2026-10-14 13:22Z [0a7a5bf4] created
