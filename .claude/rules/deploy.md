---
description: Full-deploy conventions for scripts/deploy.py and its completeness test
globs: scripts/deploy.py,scripts/verify_deploy.py,tests/test_deploy_completeness.py,docs/deployment.md
---

- Deploy is always `python3 scripts/deploy.py`. Don't hand-roll `pip install -e . && systemctl restart <service>`, because that skips skills, the vendored `episodic-memory/` and config mirrors. `--dry-run` shows every action and changes nothing. `--skip-restart` runs everything except service restarts.
- A failed step fails the whole deploy. There is no partial success. The run ends with `scripts/verify_deploy.py`, and deploy uses its exit code.
- Running councils survive an engine restart: the engine suspends them on shutdown and the next one resumes them (`docs/consultants.md` "Engine restarts"). `step_services` reads `suspends_on_shutdown` from the engine's `/v1/health` (on the engine's own `service.http_port`, never the smart-start forwarder). If it's true, the engine restarts under running councils and the deploy names them. An engine without the flag (older code) is not restarted under a council with `status == "running"`: the deploy names the runs, leaves the engine on the old code and fails, because that restart would kill them. Other units restart as usual. Paused and tool-waiting councils count as running.
  ```bash
  python scripts/deploy.py --wait-for-councils 3600  # re-ask every 30 s, restart once none run
  python scripts/deploy.py --kill-councils           # restart anyway; an older engine loses them
  ```
- `--dry-run` reports which councils would block. An engine that doesn't answer is restarted, since it can't be serving a run. Hosts without a consultants unit (pandorum runs the smart-start forwarder) are unaffected.
- A unit counts as the consultants engine when its unit file (in `/etc/systemd/system` or `~/.config/systemd/user`) contains `consultants.server`. If the unit file can't be read, the check falls back to the unit name.
- Add a new deployable artifact class to `tests/test_deploy_completeness.py` first. That test also asserts that `step_episodic(` comes before `step_services(`.
- Restart the Claude Code session after deploying a skill change. `SKILL.md` is read at session start.
- Runbook: `docs/deployment.md` ("Verify" and the running-councils paragraph).
