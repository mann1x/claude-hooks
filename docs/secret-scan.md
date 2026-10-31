# Commit secret gate

Every commit to claude-hooks passes two checks before it is made, and
every pushed commit is scanned again in CI.

```bash
python scripts/secret_scan.py --staged        # what the pre-commit hook runs
python scripts/secret_scan.py --history       # every commit (CI does this)
python scripts/secret_scan.py --install-hook  # wire --staged into .git/hooks/pre-commit
```

## Why it exists

On 2026-05-07, commit `c2f2caa` added two backups of
`config/claude-hooks.json` to the repository, one of them holding the
pgvector DSN with its password, and pushed them to the public GitHub
repo. `939a5cb` deleted the files two minutes later, but they stay in
history. Two things failed:

- **No gate ran.** The pre-commit hook ran only Caliber.
- **gitleaks' stock rules would not have stopped it.** Scanned with the
  default ruleset, all 1,034 commits come back clean, because no default
  rule reads a password inside `scheme://user:password@host`.

Commit `781ea3a` later used 32 of the password's 40 characters as a test
fixture. That too is in history, and nothing flagged it.

## The two checks

1. **gitleaks with `.gitleaks.toml`.** This is the full default ruleset
   plus three rules for what configuration files carry:

   | rule | catches |
   |---|---|
   | `url-credentials` | a password in any URL: DSNs, broker URLs, basic auth. The host may sit in the next string fragment. |
   | `config-secret-value` | a key whose last segment names a secret (`password`, `token`, `api_key`, `client_secret`, ...) with a literal value, in TOML / JSON / YAML / INI / env / unit / conf files and in backups of them. `max_tokens = 32768` does not match. |
   | `local-config-or-key-file` | committing the file at all: copies or backups of `config/claude-hooks.json`, `consultants.toml`, `consultants-config.toml`; `.env`; `*.pem` / `*.key` / `id_*`; `*-api-key`; `.netrc` / `.pgpass` / `.npmrc` / `.pypirc`. The `*.example.*` templates are allowed. |

   Placeholders pass: `<password>`, `${PGPASSWORD}`, `YOUR_PASSWORD`,
   `changeme`, paths (`key_file = "~/.claude/ollama-api-key"`), URLs without
   credentials, and fakes carrying `test-only` / `not-a-real`.

2. **This host's own secrets.** The script reads the values from where
   they live:
   - DSN passwords and secret-named values in `config/claude-hooks.json`,
     `.claude-hooks/consultants.toml`, `~/.claude/consultants-config.toml`,
     `~/.claude.json` and `~/.claude/settings*.json`;
   - every file named by a `key_file`;
   - `~/.claude/*api-key*`, `~/.claude/*.key` and `~/*.key`;
   - secret-named environment variables.

   It then searches every added line for any 12-character run of one of
   them, so a prefix, or a value split across two strings, is found too.
   No rule can do this, because a rule doesn't know the values. A finding
   names the file, the line and where the secret lives. The value is never
   printed.

The gate **fails closed**: if gitleaks isn't installed, the commit is
refused. To bypass it on purpose, use `git commit --no-verify`, and CI
will still scan the commit.

## When a commit is refused

Remove the secret from the commit. Don't add the finding to
`.gitleaksignore` or to an allowlist to get it through.
`.gitleaksignore` lists only the findings already in public history,
which the owner accepted on 2026-10-25 (internal LAN, single user).

## Deployment

`.git/hooks` isn't tracked, so `scripts/deploy.py` (`step_repo`)
installs the gate on every host. It replaces an older version of its
block in place. `scripts/verify_deploy.py` (`check_secret_gate`) FAILs
when the hook is missing or gitleaks is absent. Every worktree shares
the hook; a worktree checked out before the script existed uses the main
checkout's copy, which scans that worktree's index.

Install gitleaks with `apt install gitleaks` or `brew install gitleaks`,
or on Windows with `winget install Gitleaks.Gitleaks`. CI uses 8.30.1,
pinned by checksum (`.github/workflows/secret-scan.yml`).

## Tests

`tests/test_secret_scan.py` covers:

- each custom rule against its own canaries, generated per run;
- placeholders that must pass;
- the history scan;
- the host-secret search;
- hook installation and upgrade.

`TestSecretGate` in `tests/test_deploy_completeness.py` checks the deploy
and verify wiring.
