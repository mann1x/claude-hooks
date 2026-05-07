---
session_id: csl-2026-05-07-0807-e7c2
created: "2026-05-07T08:07:05"
question: Confirm the consultants pipeline is live by responding with one short sentence.
models:
  planner: "kimi-k2.6:cloud"
  researcher: "kimi-k2.6:cloud"
  synthesizer: "kimi-k2.6:cloud"
topology: council
effort: medium
duration_seconds: 205.09
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/claude-hooks
---

The consultants pipeline is live as a local-only systemd user service on `127.0.0.1:38095` (`consultants/cli.py:35`; `systemd/claude-hooks-consultants.service:8-10`).
