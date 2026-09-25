---
description: Per-model LLM sampling templates for consultants / get-advice / bench / coder routing
globs: claude_hooks/model_sampling.py,config/model-sampling.json
---

- Per-model sampling params (temperature, penalties) live in `config/model-sampling.json` (shipped, release-managed) and are loaded by `claude_hooks/model_sampling.py`. Do NOT hard-code temperatures at call sites — a cloud tag otherwise runs on the provider default.
- Three layers merge field-by-field: shipped `config/model-sampling.json` → user `model_sampling.templates` in `config/claude-hooks.json` (never overwritten by install) → process env `CLAUDE_HOOKS_MODEL_SAMPLING`.
- `glm-5.3*` and `deepseek-v4.1-flash*` run at temperature `0.7` (measured; cloud default 1.0 answered poorly).
- Shipped coder routes use `deepseek-v4.1-flash` / `glm-5.3-flash`; update the matching template in `config/model-sampling.json` when a route's model changes.
- Never edit `config/model-sampling.json` for a local override — put overrides under `model_sampling.templates` in `config/claude-hooks.json`.
- Tests: `tests/test_model_sampling.py`. Reference: `docs/model-sampling.md`; measured baselines in `docs/benchmarks/judge-and-sampling.md`.
