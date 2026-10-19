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
- Ollama Cloud used directly (`ollama.url` / an endpoint `https://ollama.com`, `/api` or `/v1`, no local Ollama): the key comes from `api_key()` / `api_key_source()` — the file named by `ollama.api_key_file` (first line) in `config/claude-hooks.json`, else `OLLAMA_API_KEY` — and is sent only when `is_cloud(base_url)` (host `ollama.com`), never to a LAN server. `ChatClient` / `OpenAICompatChatClient` take it when built for ollama.com without a key; `[endpoints.X] key_file` wins (`endpoint_api_key()`), an inline `api_key` is read with a warning. `auth_headers()` adds it in the caliber proxy and `ctx_probe`. Services never read a shell profile, so the host setting is the file (`~/.claude/ollama-api-key`, mode 0600 / user-only ACL), never the key in a config. `--explain` and `verify_deploy` (`check_ollama_cloud_key`, FAIL when the engines' server is ollama.com with no key) name the source, never the key; `tests/conftest.py` closes `_key_config` so no test sees the host key. On ollama.com every model is a cloud call (`ollama_slots.is_cloud`) and the plan is read from its `/api/me` with the key. Tests: `tests/test_ollama_cloud_direct.py`.
- `install.py` asks for `ollama.url` with the current value as default; non-interactive runs write nothing.
- `scripts/verify_deploy.py` reports the URL, its source and whether it answers, and WARNs when the hooks name a remote Ollama while the engines fall back to `localhost` (the upgraded-host case).
- Tests: `tests/test_ollama_upstream.py`, `tests/test_get_advice_ctx_probe.py`. Docs: `docs/consultants.md`, `docs/caliber-proxy.md`.
