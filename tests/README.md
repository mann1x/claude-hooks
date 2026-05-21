# claude-hooks test suite

Run with:

```bash
make test                                              # all tests
make test ARGS="-k <pattern>"                          # subset
/root/anaconda3/envs/claude-hooks/bin/python -m pytest # direct
```

The suite **must** run in the `claude-hooks` conda env — system Python
3.9 lacks `h2` and produces ~18 spurious proxy-test failures. The
`conftest.py:pytest_configure` hook prints a loud warning if you run
under any other interpreter.

---

## Network identifiers in fixtures

Tests that mention hosts, URLs, or DSNs **never** make real network
calls in unit-test mode — every such call goes through a mocked HTTP /
DB layer. But the *values* still need to live somewhere.

**Rule:** never hard-code a real LAN identifier in a test file. Pull
it from `tests/_fixtures_net.py` instead.

```python
from tests._fixtures_net import (
    FIXTURE_LAN_HOST,
    FIXTURE_OLLAMA_PROXY_GENERATE,
    FIXTURE_PG_DSN,
    FIXTURE_PROXY_URL,
)

cfg = {"hyde_url": FIXTURE_OLLAMA_PROXY_GENERATE}
```

The defaults are drawn from
[RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737) documentation
blocks (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) so they
are obviously fake to anyone reading the test and cannot collide with
any real network.

### Overriding for live-integration runs

A small number of tests (notably `tests/test_pgvector_integration.py`)
actually probe live infrastructure. They use the same constants from
`_fixtures_net.py`, which means you can point them at your real infra
in **two** ways — both gitignored, both safe to put real secrets in:

#### Option 1 — environment variables (CI-friendly)

```bash
export CLAUDE_HOOKS_TEST_LAN_HOST=192.168.1.50
export CLAUDE_HOOKS_TEST_PG_DSN=postgresql://me:pw@db.local:5432/memory
make test
```

Every constant in `_fixtures_net.py` is overridable. See the source
for the full list — they all share the prefix `CLAUDE_HOOKS_TEST_*`.

#### Option 2 — local override file (developer-friendly)

Drop a `tests/.env.local` file:

```dotenv
# tests/.env.local — gitignored, never committed
CLAUDE_HOOKS_TEST_LAN_HOST=192.168.1.50
CLAUDE_HOOKS_TEST_PG_DSN=postgresql://me:pw@db.local:5432/memory
CLAUDE_HOOKS_TEST_OLLAMA_DIRECT_EMBEDDINGS=http://ollama.local:11434/api/embeddings
```

`_fixtures_net.py` reads this file at import time and applies any keys
not already set in `os.environ`. **Process environment wins** over
the file, so CI can override the developer override.

The file is in `.gitignore` so it can never accidentally be committed.

### What about model names and table names?

Public model identifiers (`qwen3-embedding:0.6b`, `gemma4:31b-cloud`,
…) are fine to write directly into tests — they're public Ollama
catalog entries, not infrastructure. The same goes for table names
like `memories_qwen3`: those are fixture-only and don't depend on real
DB state.

---

## Smoke tests

Every shared fixture in `conftest.py` has a corresponding test in
`tests/test_fixtures.py`. If those pass, downstream tests can rely on
the fixtures' shape without re-checking invariants.

## Markers

- `@pytest.mark.parity` — M12 parity cohort; opted into via
  `pytest -m parity`.
- `@pytest.mark.integration` — live-infrastructure tests; opt out via
  `pytest -m "not integration"`.
