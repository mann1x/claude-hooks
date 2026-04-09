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
