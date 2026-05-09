# Reference baseline — caliber init via claude-cli

This is the **target** for the gemma4-98e-via-grounding-proxy gap-closure
work. Snapshotted on 2026-04-29 against an isolated copy of claude-hooks
@ `b3fcb1f` so the gemma run can target the same artifact shape.

## Run setup

| Field | Value |
|---|---|
| Eval workspace | `/srv/dev-disk-by-label-opt/dev/caliber-eval/claude-cli/` |
| Source | `claude-hooks` @ commit `b3fcb1f` (this repo, with `.claude/`, `.caliber/`, `CLAUDE.md`, `.cursor/`, `.agents/`, `.opencode/`, `.coverage`, `.env`, `.claudemem/`, `.axon/`, `.wolf/`, `bench-*.json`, `config/claude-hooks.json` stripped) |
| Caliber | `1.48.2` |
| Provider | `claude-cli` (via `CALIBER_USE_CLAUDE_CLI=1`) |
| Model | `default` (inherited from Claude Code session) |
| Command | `caliber init --auto-approve --show-tokens --verbose --agent claude` |
| Env | `CALIBER_CLAUDE_CLI_TIMEOUT_MS=1800000`, `CALIBER_GENERATION_TIMEOUT_MS=1800000`, `CALIBER_STREAM_INACTIVITY_TIMEOUT_MS=600000` |

> **First-attempt failure:** the default `CALIBER_STREAM_INACTIVITY_TIMEOUT_MS=120000` is
> too short for caliber init's skill-generation calls — the run died at 2m9s on
> stream-inactivity. The `caliber-smart` wrapper sets this to 600000 (10min); the
> baseline retry below uses the same value.

## Wall time

**`Done in 37m 14s`** — caliber-reported total. Process etime ≈ 40min.

| Phase | Duration |
|---|---|
| Detecting project stack | 12s |
| Searching community skills | 50s (10 found) |
| Generating configs | 9m 15s |
| Generating skills | 11m 13s — 5 project skills produced |
| Validating & refining config | **16m 33s** (longest tail; 3 scoring issues, "References" category) |

The Validating phase is dominated by the `score-refine` loop — caliber identifies
"3 scoring issues: References" and re-prompts the model to add citations to
specific files. With claude-cli that converges; with gemma4-98e it likely
needs more rounds or different framing.

## Score

**`94/100`** (caliber's `score-refine.ts` rubric).

## Output artifacts

`reports/claude-cli-artifacts/` snapshot (17 MB):
- `CLAUDE.md`
- `dotclaude/` — full `.claude/` tree (rules, skills, commands, agents)
- `init-output.log` — raw caliber stdout

### Files written (caliber's own report)

```
✓ CLAUDE.md
✓ .claude/rules/testing-conventions.md
✓ .claude/rules/provider-conventions.md
✓ .claude/rules/hook-conventions.md
✓ .claude/rules/proxy-conventions.md
✓ .claude/skills/add-provider/SKILL.md
✓ .claude/skills/add-hook-handler/SKILL.md
✓ .claude/skills/run-tests/SKILL.md
✓ .claude/skills/proxy-metadata-field/SKILL.md
✓ .claude/skills/code-graph-rebuild/SKILL.md
✓ .claude/skills/find-skills/SKILL.md          ← caliber built-in
✓ .claude/skills/save-learning/SKILL.md         ← caliber built-in
✓ .claude/skills/setup-caliber/SKILL.md         ← caliber built-in
```

5 project-specific skills + 4 conventions + 3 caliber built-ins.

## Metrics that matter for the gemma comparison

Computed by `score.py`:

| Metric | Value |
|---|---|
| Total skills | 8 |
| Project-specific skills (excl. built-ins) | 5 |
| Skills with `paths:` frontmatter populated | **5/5** (100% of project skills) |
| Total path-glob entries across `paths:` blocks | 25 |
| `file:line` references in skill bodies | **0** |
| Bare file references (`` `name.py` `` style) in skill bodies | 48 |
| Body chars (project skills): min / median / max | 6,554 / 9,186 / 10,046 |
| Body chars (built-ins): typical | ~2,000 |

**Surprise finding for the gemma re-test:** "no `file:line` references" was
the user's complaint about the prior gemma run. Claude-cli also produces zero
`file:line` references — caliber's prompt template asks for **file-level**
`paths:` frontmatter and bare file mentions in body prose, not line-level
citations. The actual gap to close on gemma is:

1. **Skill count** — match 5 project skills (gemma had been producing fewer).
2. **`paths:` frontmatter populated on every project skill** (with project-specific globs, not just `**/*.py`).
3. **Bare file refs in body prose** — claude-cli weaves ~10 per skill into
   step-by-step instructions ("edit `proxy/metadata.py`", "in `_log_line`...").
4. **Density of project-specific facts** — claude-cli cites concrete numbers
   (the 549/16 test count, 3ms cold-start budget, `SCHEMA_VERSION` bumps).
   This requires the LLM to actually *read* and *retain* details from
   project files, not just summarize their structure.

## Per-skill detail (project-specific only)

| Skill | Body chars | Paths in fm | Bare file refs |
|---|---:|---:|---:|
| `add-provider` | 10,046 | 4 | 5 |
| `add-hook-handler` | 9,648 | 7 | 9 |
| `code-graph-rebuild` | 9,186 | 3 | 6 |
| `proxy-metadata-field` | 8,924 | 6 | 6 |
| `run-tests` | 6,554 | 5 | 21 |

`run-tests` has the most file references because it cites specific test files
(`test_proxy.py`, `test_proxy_p1.py`, etc.) by name throughout.

### Skill: `add-provider` (sample frontmatter)

```yaml
---
name: add-provider
description: |
  Scaffolds a new memory backend under claude_hooks/providers/<name>.py
  implementing the Provider ABC. Use when user says 'add provider', 'new
  memory backend', 'support <db> for memory/recall', or wires a new vector
  store into claude-hooks. Generates provider class, REGISTRY entry,
  DEFAULT_CONFIG block, example config snippet, and gated integration test.
  Do NOT use for modifying existing qdrant/memory_kg/pgvector/sqlite_vec
  providers, for changing recall pipeline logic, or for adding new hook
  events (use add-hook-event for that).
paths:
  - claude_hooks/providers/**/*.py
  - claude_hooks/config.py
  - config/claude-hooks.example.json
  - tests/test_*_integration.py
---
```

The description is **dense** — it tells Claude when to invoke (3 user phrases
listed verbatim), what to produce (5 specific deliverables), and when NOT to
invoke (cross-references to two other skills). This is the level of
specificity gemma needs to reach.

## Side-effect on the run: claude-hooks recall asymmetry

Each `claude -p` subprocess caliber spawned inherited
`~/.claude/settings.json` and ran the claude-hooks recall pipeline against
pgvector via Ollama embeddings. So this baseline includes the implicit
tailwind of prior project memories injected into every skill-generation
prompt. The gemma run through the grounding proxy should be given the
**same** advantage (Option C — server-side `pgvector` recall prepend +
`recall_memory` tool — see `docs/caliber-proxy.md` work plan) for a
fair comparison.

## How to reproduce

```bash
EVAL=/srv/dev-disk-by-label-opt/dev/caliber-eval
mkdir -p "$EVAL"
rsync -a --delete \
    --exclude=.claude --exclude=.wolf --exclude=node_modules \
    --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=bench-qwen3.json --exclude=bench-results.json \
    --exclude='*.bak-*' \
    /srv/dev-disk-by-label-opt/dev/claude-hooks/ "$EVAL/claude-cli/"
cd "$EVAL/claude-cli"
rm -rf .caliber .claudemem .axon graphify-out config/claude-hooks.json \
       .cursor .agents .opencode .github/instructions
rm -f CLAUDE.md AGENTS.md CALIBER_LEARNINGS.md .cursorrules \
      .github/copilot-instructions.md .claudemem-reindex.lock \
      .coverage .env

CALIBER_USE_CLAUDE_CLI=1 \
CALIBER_CLAUDE_CLI_TIMEOUT_MS=1800000 \
CALIBER_GENERATION_TIMEOUT_MS=1800000 \
CALIBER_STREAM_INACTIVITY_TIMEOUT_MS=600000 \
caliber init --auto-approve --show-tokens --verbose --agent claude
```

## Score the result

```bash
/root/anaconda3/envs/claude-hooks/bin/python /srv/dev-disk-by-label-opt/dev/caliber-eval/score.py \
    /srv/dev-disk-by-label-opt/dev/caliber-eval/claude-cli claude-cli-baseline \
    > /srv/dev-disk-by-label-opt/dev/caliber-eval/reports/claude-cli-baseline.json
```

Diff against the gemma run with `jq` once both JSONs exist.
