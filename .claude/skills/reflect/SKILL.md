# /reflect — Memory Pattern Synthesis

Analyze recent Qdrant memories for recurring patterns and generate CLAUDE.md rules.

## Instructions

Run the reflect command via the claude-hooks conda environment:

```bash
/root/anaconda3/envs/claude-hooks/bin/python -m claude_hooks.reflect
```

This will:
1. Pull recent memories from all providers (Qdrant + Memory KG)
2. Group by observation type (fix, preference, decision, gotcha)
3. Call Ollama (via the proxy at 192.168.178.2:11433) to identify recurring patterns
4. Append new rules to ~/.claude/CLAUDE.md

To preview without writing:

```bash
/root/anaconda3/envs/claude-hooks/bin/python -m claude_hooks.reflect --dry-run
```

Show the user the generated rules and confirm they were written.

## When to use

- When the user asks to reflect, synthesize, or review recent learnings
- After a long session with many fixes or decisions
- Periodically (e.g., weekly) to distill accumulated knowledge
