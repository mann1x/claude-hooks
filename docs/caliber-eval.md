# caliber-eval — gap-closure benchmark for the caliber-grounding-proxy

The caliber-eval workspace measures whether the caliber-grounding-proxy
(`claude_hooks/caliber_proxy/`) plus a given LLM backend can match
caliber driven by `claude-cli` on the four rubric axes that gemma
historically lagged on (skill count, populated `paths:` frontmatter,
file references in skill bodies, score-refine convergence).

## Where to read

- **Summary results (versioned, ships with the code)** —
  [`docs/caliber-eval-results/`](caliber-eval-results/) in this repo.
  One `<label>.json` (scorer output) + one `<label>-summary.md`
  (narrative comparison vs baseline) per bench. Index + per-label
  one-liner table at [`caliber-eval-results/README.md`](caliber-eval-results/README.md).
  This is what a future reader looks at first.
- **Full workbench (live, off-repo)** —
  `/srv/dev-disk-by-label-opt/dev/caliber-eval/`. Holds the rsynced
  project workspaces, run logs, fake-HOME isolation dirs, and the
  full `claude-cli-artifacts/` snapshot (17 MB) used as ground truth
  for diffs. Read here when you need to actually rerun a bench, look
  at a transcript, or diff the on-disk artefacts a model produced.

Workbench docs:

- [`PROTOCOL.md`](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md)
  — full reproduce recipe (workspace prep, fake-HOME isolation, dedicated
  proxy, run launcher, scoring, **§7 publish summary results back to this
  repo**, tear down). Read first.
- [`README.md`](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/README.md)
  — entry point + directory layout + TL;DR.
- `reports/<label>-summary.md` + `reports/<label>.json` — the
  authoritative copies (the in-repo `caliber-eval-results/` versions are
  copies of these, refreshed after each run per PROTOCOL §7).

Why split workbench-vs-results: the workspace dirs (`gemma4-31b-cloud/`
etc.) are 250+ MB rsync'd copies of claude-hooks (with `.git`); logs
accumulate per run; `claude-cli-artifacts/` is 17 MB. None of that
belongs under version control. The lightweight `<label>.json` +
`<label>-summary.md` pair (a few KB each) **does** belong with the
code and is committed via [PROTOCOL §7](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md).

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
