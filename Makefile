# Project Makefile — pin tooling to the claude-hooks conda env so we
# never accidentally run pytest under the system Python (which lacks
# httpx[http2]/h2 and produces 18+ spurious proxy-test failures).
#
# Override CHPY=... if your env lives somewhere else.

CHPY ?= /root/anaconda3/envs/claude-hooks/bin/python

.PHONY: help test test-fast test-cov code-graph install-dev check-env

help:
	@echo "Common targets (all use $(CHPY)):"
	@echo "  make test         — full test suite"
	@echo "  make test-fast    — full suite with -x and short tracebacks"
	@echo "  make test-cov     — with coverage report"
	@echo "  make code-graph   — rebuild graphify-out/ for this repo"
	@echo "  make install-dev  — install dev requirements into the env"
	@echo "  make check-env    — verify the conda env exists & has key deps"

check-env:
	@test -x "$(CHPY)" || { echo "ERROR: $(CHPY) missing — install the claude-hooks conda env"; exit 1; }
	@$(CHPY) -c "import h2, pytest, httpx" 2>/dev/null || { \
		echo "ERROR: env missing required deps (h2/pytest/httpx); run: make install-dev"; exit 1; }
	@echo "OK: $$( $(CHPY) --version ) at $(CHPY)"

test: check-env
	$(CHPY) -m pytest tests/ -q

test-fast: check-env
	$(CHPY) -m pytest tests/ -x --tb=short -q

test-cov: check-env
	$(CHPY) -m pytest tests/ --cov=claude_hooks --cov-report=term-missing -q

code-graph: check-env
	$(CHPY) -m claude_hooks.code_graph build --root . --quiet
	@$(CHPY) -m claude_hooks.code_graph info --root .

install-dev:
	$(CHPY) -m pip install -r requirements-dev.txt
