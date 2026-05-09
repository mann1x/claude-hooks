# caliber-eval — gap-closure benchmark for the caliber-grounding-proxy

The caliber-eval workspace measures whether the caliber-grounding-proxy
(`claude_hooks/caliber_proxy/`) plus a given LLM backend can match
caliber driven by `claude-cli` on the four rubric axes that gemma
historically lagged on (skill count, populated `paths:` frontmatter,
file references in skill bodies, score-refine convergence).

The benchmark, baseline data, scorer, run logs, and per-bench reports
all live **outside this repo** at:

    /srv/dev-disk-by-label-opt/dev/caliber-eval/

Docs in that directory:

- [`PROTOCOL.md`](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md)
  — full reproduce recipe (workspace prep, fake-HOME isolation, dedicated
  proxy, run launcher, scoring, comparison). Read first.
- [`README.md`](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/README.md)
  — entry point + directory layout + TL;DR.
- `reports/REFERENCE-claude-cli.md` — the 2026-04-29 baseline (claude-hooks
  @ `b3fcb1f`, 8 skills, score 94/100, wall 37m 14s) that any non-claude-cli
  model is graded against.
- `reports/<label>.json` — scorer output per benchmarked workspace.

Why outside the repo: the workspace dirs (`gemma4-31b-cloud/` etc.) are
17+ MB rsync'd copies of claude-hooks itself, which would balloon the
repo. The workspace + report layout is workbench-style (logs, scratch,
fake-home dirs, transient artefacts) that doesn't belong under version
control.

## Cross-references in this repo

- Proxy implementation: [`claude_hooks/caliber_proxy/`](../claude_hooks/caliber_proxy/)
- Proxy runtime docs: [`docs/caliber-proxy.md`](caliber-proxy.md)
- Cloud-resilience port plan: [`docs/PLAN-caliber-proxy-cloud-resilience.md`](PLAN-caliber-proxy-cloud-resilience.md)
- Past gemma model engineering notes: [`docs/gemma4-tool-use-notes.md`](gemma4-tool-use-notes.md)
- Modelfile attempts (v1..v5): [`modelfiles/`](../modelfiles/)

## When this comes up

- "Did our latest proxy/model change improve / regress caliber init quality?"
  → run a new label per `caliber-eval/PROTOCOL.md`, score, diff against
  `reports/claude-cli-baseline.json`.
- "Why does caliber init through the proxy stall on cloud models?"
  → the proxy's pre-resilience-port behavior was to surface upstream 5xx
  blips immediately. See `PLAN-caliber-proxy-cloud-resilience.md` for
  the port that fixes it.
- "How do I run a bench without touching the live caliber config?"
  → `PROTOCOL.md` §3 (per-bench fake-HOME pattern; `/root/.caliber/config.json`
  otherwise overrides every env var).
