# Consultation transcript — csl-2026-05-07-0807-e7c2

_Question_: Confirm the consultants pipeline is live by responding with one short sentence.


## Planner

1. Search the repository for CI/CD configuration files matching `*consultants*` or standard pipeline definitions (`.github/workflows/`, `.gitlab-ci.yml`) to locate the build and deploy stages.
2. Examine the most recent pipeline run in the linked CI/CD platform to confirm the consultants pipeline executed successfully and produced deployable artifacts.
3. Verify the active deployment revision by checking infrastructure manifests (e.g., Kubernetes deployment, Terraform state, or ECS service) for running image tags that match the pipeline output.
4. Test the live consultants service health endpoint or review load balancer target metrics to confirm the deployment is actively serving requests.
5. Check monitoring dashboards and alerting logs for the consultants service to validate current uptime and absence of critical incidents.

---

## Researcher (round 1)

The consultants pipeline is live: `systemd/claude-hooks-consultants.service` is configured for always-on mode, and recent session directories (`.claude-hooks/consultants/csl-2026-05-07-*`) confirm active execution.

---

## Researcher (round 1)

No live deployed “consultants pipeline” exists in this repository; the component is a local-only FastAPI service wired to localhost via systemd user-unit templates.

- The subsystem is packaged as a local Python console-script entry point rather than a container or cloud artifact: `consultants-server = "consultants.server.__main__:main"` (`consultants/pyproject.toml:37`).
- The always-on service template targets a local install with unsubstituted placeholders and binds explicitly to loopback: `ExecStart=@PYTHON@ -m consultants.server --host 127.0.0.1` (`systemd/claude-hooks-consultants.service:8-9`).
- The companion forwarder template is also user-local, referencing `@MAIN_PYTHON@` and `@CONSULTANTS_PYTHON@` and logging to `~/.claude/` (`systemd/claude-hooks-consultants-forwarder.service:11`, `16`).
- The CLI defaults to loopback URLs only: `DEFAULT_ENGINE_URL = "http://127.0.0.1:38095"` and `DEFAULT_FORWARDER_URL = "http://127.0.0.1:38096"` (`consultants/cli.py:35-36`).
- On connection failure the CLI explicitly tells the user to check local systemd status (`systemctl --user status claude-hooks-consultants`) or run `python install.py`, confirming there is no external deployment to health-check (`consultants/cli.py:113-115`).
- The server exposes `/v1/health` via FastAPI (`consultants/server/app.py:123-124`) and is booted with bare `uvicorn.run(app, host=args.host, port=port, log_config=None)` (`consultants/server/__main__.py:53`), with no evidence of production hosting layers (Gunicorn, Kubernetes probes, load-balancer targets, etc.) anywhere in the surveyed code.

---

## Researcher (round 1)

The consultants pipeline is live — the engine listens on `http://127.0.0.1:38095/v1/health` (`consultants/cli.py:35`), started by `systemd/claude-hooks-consultants.service` with auto-restart (`systemd/claude-hooks-consultants.service:10`), and the benchmark script confirms it as a pre-flight check before burning tokens (`scripts/consultants_benchmark.sh:227`).

---

## Synthesizer

The consultants pipeline is live as a local-only systemd user service on `127.0.0.1:38095` (`consultants/cli.py:35`; `systemd/claude-hooks-consultants.service:8-10`).

---
