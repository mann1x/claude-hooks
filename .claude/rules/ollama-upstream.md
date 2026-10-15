---
description: Which Ollama server the consultants engine, get-advice and the caliber grounding proxy use — one resolver, no shipped host
globs: claude_hooks/ollama_upstream.py,claude_hooks/config.py,claude_hooks/caliber_proxy/ollama.py,claude_hooks/get_advice/ctx_probe.py,consultants/cli.py,consultants/server/runner.py,bin/caliber-smart,bin/caliber-grounding-proxy,install.py,scripts/verify_deploy.py,config/claude-hooks.example.json
---

- `claude_hooks/ollama_upstream.py` is the only place that decides the Ollama server for `/consultants` (engine + `consultants/cli.py`), `/get-advice` (`claude_hooks/get_advice/ctx_probe.py`), the caliber grounding proxy (`claude_hooks/caliber_proxy/ollama.py`) and `bin/caliber-smart`. Call `url()` / `resolve()`; never add a per-component fallback.
- Order (`resolve()` returns `(url, source)`): `CALIBER_GROUNDING_UPSTREAM` (a trailing `/v1` dropped by `normalise()`) → `OLLAMA_HOST` only when it starts with `http://` / `https://` (a bind address such as `0.0.0.0` is ignored) → `ollama.url` in `config/claude-hooks.json` → `DEFAULT_URL` (`http://localhost:11434`).
- No host address is ever shipped: `ollama.url` defaults to `""` in `claude_hooks/config.py` `DEFAULT_CONFIG` and in `config/claude-hooks.example.json`. Code, shims and operator scripts in `scripts/` carry no LAN default; `test_no_lan_address_in_shipped_code` enforces it.
- Shell shims read the URL with:
  ```bash
  python -m claude_hooks.ollama_upstream            # prints the URL
  python -m claude_hooks.ollama_upstream --explain  # URL plus the step that answered
  ```
- `install.py` asks for `ollama.url` with the current value as default; non-interactive runs write nothing.
- `scripts/verify_deploy.py` reports the URL, its source and whether it answers, and WARNs when the hooks name a remote Ollama while the engines fall back to `localhost` (the upgraded-host case).
- Tests: `tests/test_ollama_upstream.py`, `tests/test_get_advice_ctx_probe.py`. Docs: `docs/consultants.md`, `docs/caliber-proxy.md`.
