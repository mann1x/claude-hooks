---
name: consolidate
description: "Memory-store cleanup: find near-duplicate memories, compress old entries, and prune stale ones across the configured providers (pgvector / sqlite_vec / Qdrant / Memory KG). Runs `python -m claude_hooks.consolidate`, with `--dry-run` to preview counts without modifying anything. Use when the user asks to clean up, deduplicate or consolidate memories, when recall keeps returning redundant near-identical hits, or as periodic (monthly) maintenance to keep the store lean."
---

# /consolidate — Memory Cleanup

Find duplicate memories, compress old entries, and prune stale ones.

## Instructions

Run the consolidate command via the claude-hooks conda environment:

```bash
/root/anaconda3/envs/claude-hooks/bin/python -m claude_hooks.consolidate
```

To preview without modifying anything:

```bash
/root/anaconda3/envs/claude-hooks/bin/python -m claude_hooks.consolidate --dry-run
```

Show the user the results: how many merged, compressed, and pruned.

## When to use

- When the user asks to clean up, deduplicate, or consolidate memories
- When Qdrant recall returns too many similar/redundant entries
- Periodically (e.g., monthly) to keep the memory store lean
