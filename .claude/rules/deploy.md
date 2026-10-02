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
- A daemon that systemd doesn't manage (a Windows scheduled task) is restarted by `_restart_unmanaged_daemon()` through `claude_hooks.daemon_ctl restart`, called in `step_services` before the `if not units:` early return, and the embedder is re-ensured (`_respawn_embedder`). A daemon that isn't running is skipped.
- Add a new deployable artifact class to `tests/test_deploy_completeness.py` first. That test also asserts that `step_episodic(` comes before `step_services(`.
- The cloud relay is on wherever `hooks.mailbox` is, unless `cloud_relay.enabled` is false, with its folder at `~/claude-mailbox` unless `cloud_relay.root` names another. `_sync_relay_instructions` (in `step_skills`) resolves it through `settings()` in `claude_hooks/mailbox/relay.py`, never by reading `cloud_relay.root` directly, creates the folder's `sessions/` dir and copies `claude_hooks/mailbox/cloud/MAILBOX.md` into it. `check_mailbox_relay` in `scripts/verify_deploy.py` FAILs when the folder's `sessions/` dir is missing or its `MAILBOX.md` is stale. Hosts without the mailbox are unaffected.
- The daemon unit is sandboxed (`ProtectSystem=strict`), so `_sync_relay_instructions` also calls `ensure_unit_grant()` from `claude_hooks/mailbox/relay.py` before `step_services`: it writes the `claude-hooks-daemon.service.d/mailbox-relay.conf` drop-in granting the relay folder, both as spelled and resolved. `check_mailbox_relay` FAILs when `missing_grants()` reports a daemon unit without it.
- Restart the Claude Code session after deploying a skill change. `SKILL.md` is read at session start.
- Runbook: `docs/deployment.md` ("Verify" and the running-councils paragraph).
