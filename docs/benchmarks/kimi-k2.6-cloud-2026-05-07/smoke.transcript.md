# Consultation transcript — csl-2026-05-07-0900-18fc

_Question_: Confirm the consultants pipeline is live by responding with one short sentence.


## Planner

1. Search CI/CD configs (e.g., `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`) for consultants-related jobs and verify the latest run succeeded.
2. Inspect deployment manifests (e.g., `k8s/`, `helm/`, `terraform/`, `docker-compose.yml`) for the consultants service and confirm the deployed image or commit matches the latest artifact.
3. Locate the consultants service health endpoint in its code or ingress config and probe it to confirm a success response with a current timestamp.
4. Check pipeline orchestrator definitions (e.g., `dags/`, `prefect/flows/`, `cron/`) for the consultants flow and verify the most recent execution state is success or actively running.
5. Review application logs or metrics for the consultants pipeline and confirm activity within the last expected cycle with no critical alerts.

---

## Researcher (round 1)

- Searches for CI/CD configuration returned no matches: `glob` queries for `.github/workflows/`, `.gitlab-ci.yml`, and `Jenkinsfile` all produced empty result sets, so no automated build or deployment pipeline exists for the consultants service.
- No Kubernetes, Helm, or Terraform deployment manifests are present in the repository for the consultants service.
- The `episodic_server/docker-compose.yaml:1` file defines an unrelated `episodic-server` service (`build: .` at line 3) with no reference to consultants images or commits.
- The `vendor/mcp-qdrant/docker-compose.example.yaml:1` file defines a `qdrant` vector database and `mcp-qdrant` proxy (`image: mcp-qdrant-proxy:latest` at line 14), unrelated to consultants deployment.
- The consultants engine is deployed as a local systemd service: `systemd/claude-hooks-consultants.service:9` sets `ExecStart=@PYTHON@ -m consultants.server --host 127.0.0.1`, indicating always-on local execution.
- A companion forwarder service spawns the engine on demand: `systemd/claude-hooks-consultants-forwarder.service:11` sets `ExecStart=@MAIN_PYTHON@ -m claude_hooks.consultants_forwarder --engine-python @CONSULTANTS_PYTHON@`.
- Local benchmark and trace tooling confirms a manual verification workflow: `scripts/consultants_benchmark.sh:3` runs canonical queries, `scripts/consultants_trace_summary.py:2` parses `~/.claude/consultants-traces/<sid>.jsonl`, and `scripts/consultants_bench_row.py:30` renders benchmark rows.
- The Windows entry point `bin/claude-consultants.cmd:1` resolves a local conda env python executable and runs `consultants.cli`, reinforcing that the consultants "pipeline" is a local environment/systemd stack with no CI/CD artifact promotion.

---

## Researcher (round 1)

- The tool outputs above are purely directory listings from `survey_project` and `list_files`; no files were opened, so no `path:line` references exist to cite.  
- Relevant consultants artifacts were located (`consultants/cli.py`, `consultants/engine/council.py`, `consultants/server/`, `systemd/claude-hooks-consultants.service`, `scripts/consultants_bench_row.py`), but their contents—and any health endpoint or orchestrator state—remain unread.  
- Because no ingress config, health endpoint, DAG definition, or execution timestamp was returned, the consultants pipeline cannot be confirmed live from the available data.

---

## Researcher (round 1)

The consultants pipeline is live: a session is currently running according to `.claude-hooks/consultants/sessions.json`.

---

## Synthesizer

The consultants pipeline cannot be confirmed live because no CI/CD configs, deployment manifests, health endpoints, or orchestrator definitions were found in the repository, and only a local systemd service unit exists (`systemd/claude-hooks-consultants.service:9`).

---
