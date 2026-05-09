# caliber-eval results — published

Versioned snapshots of caliber-eval bench reports. The full workbench
(rsynced project workspaces, run logs, fake-HOME dirs, scratch state)
lives outside the repo at `/srv/dev-disk-by-label-opt/dev/caliber-eval/`;
this directory holds **only the lightweight summary artefacts** that
are useful to ship with the code:

- `<label>.json` — `score.py` output for one bench (skill counts,
  `paths:` frontmatter coverage, body chars, per-skill detail).
- `<label>-summary.md` — narrative comparison summary written after
  scoring (run setup, headline metrics vs baseline, where the
  gap comes from, verdict).
- `REFERENCE-claude-cli.md` + `claude-cli-baseline.json` — the
  reference target every other label is graded against.

## Index

| Label | Date | Model | Wall | Score | Skills (proj/total) | Notes |
|---|---|---|---|---|---|---|
| [claude-cli (REFERENCE)](REFERENCE-claude-cli.md) | 2026-04-29 | claude-cli (default) | 37m 14s | **94/100** | 5/8 | Baseline; every other label is graded against this |
| `gemma-tools-optc` | 2026-04-29 | gemma4-98e:tools | ~30m | n/a | 0/3 | Pre-resilience-port; produced 3 skills with empty `paths:` fm. Smoke evidence only |
| [gemma4-31b-cloud](gemma4-31b-cloud-summary.md) | 2026-05-09 | gemma4:31b-cloud | **28m 41s** | **85/100** | **2/5** | First non-claude-cli A grade; 23% faster than baseline; `paths:` fm 2/2 |

## How these get published

After a bench completes and is scored (per
[`PROTOCOL.md` §5](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md)),
the operator copies `<label>.json` and `<label>-summary.md` from the
workbench's `reports/` dir into this directory and commits them. The
heavy artefacts (`claude-cli-artifacts/` snapshot, `logs/`, per-bench
workspaces) stay outside the repo.

Step encoded in
[`caliber-eval/PROTOCOL.md` §7](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md).

## Pointer back to the workbench

- Full reproduce recipe: [`/srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md`](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md)
- In-repo overview: [`docs/caliber-eval.md`](../caliber-eval.md)
- Cloud-resilience port: [`docs/PLAN-caliber-proxy-cloud-resilience.md`](../PLAN-caliber-proxy-cloud-resilience.md)
- Proxy runtime docs: [`docs/caliber-proxy.md`](../caliber-proxy.md)
