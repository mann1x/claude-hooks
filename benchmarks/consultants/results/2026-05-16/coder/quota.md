# Ollama Pro quota tracking — 2026-05-16 coder skill-eval

This file logs Ollama Pro quota readings before/after each phase
of the run so the team can correlate token spend with actual
weekly-quota consumption.

## Smoke run (trivial tier × 4 models = 8 trials, ~200K tokens estimated)

### Pre-smoke (recorded 2026-05-16, before run start)

- **Session usage**: 0%
- **Weekly usage**: 4%

### Post-smoke (= pre-full, recorded 2026-05-16)

- **Session usage**: 0.2%
- **Weekly usage**: 4%

Smoke delta: session 0% → 0.2%, weekly unchanged (4%). 8 trials
(6 PASS + 2 ERROR on the wrong `qwen3-next:cloud` name). Total
token spend ≈ 10.3K (6 passing trials × ~1700 tok/trial).

## Full run (32 trials, ~1.42M tokens estimated)

### Pre-full

Same readings as post-smoke above — full run fires
immediately after.

- **Session usage**: 0.2%
- **Weekly usage**: 4%

Model substitution: `qwen3-next:cloud` (404, wrong) →
`qwen3-coder-next:cloud` (the actual coder variant; previous
guesses `qwen3-next:cloud` and `qwen3-next:80b-cloud` were both
wrong — see [[reference_ollama_pro_cloud_names]]).

### Post-full (recorded 2026-05-16)

- **Session usage**: 1.6%
- **Weekly usage**: 4.3%

Full-run deltas:
- Session: 0.2% → 1.6% (+1.4 pp for 32 trials + judge calls)
- Weekly: 4.0% → 4.3% (+0.3 pp)

32 trials × ~2K tokens each + 32 judge calls × ~1.5K tokens =
~64K trial tokens + ~48K judge tokens ≈ 112K total. Well under
the 1.42M pre-run envelope estimate (estimator's
unknown-model defaults bake in 5 iters + larger prompt/
completion budgets than the real run used).

## Summary

| Phase | Trials | Session before | Session after | Δ session | Weekly before | Weekly after | Δ weekly |
|---|---|---|---|---|---|---|---|
| Smoke (trivial × 4) | 8 (6P + 2E)| 0% | 0.2% | +0.2 | 4% | 4% | 0 |
| Full (all tiers × 4) | 32 (all PASS) | 0.2% | 1.6% | +1.4 | 4% | 4.3% | +0.3 |
| **Total M11b 2026-05-16** | **40** | **0%** | **1.6%** | **+1.6** | **4%** | **4.3%** | **+0.3** |
