# Releasing claude-hooks

This document captures the branch model and the cut procedure. It is
the **authoritative source** — if README or CHANGELOG drift, this file
wins.

## Branch model

```
                              tag: v1.0.0
                                  │
   main   ────●────●────●────●────●────●────●─►   (release branch)
              ▲                   ▲
              │  merge on cut     │  merge on cut
              │                   │
   dev    ────●────●────●─────────●────────────►  (working branch)
                  feature/fix/exp commits land here
```

- **`main`** — release branch. Every commit on `main` is shippable.
  Tags live here. CI (when added) gates merges to `main`.
- **`dev`** — working branch. All feature work, refactors, doc
  changes, dependency bumps, and exploratory work land on `dev`
  first. Push freely.
- **Topic branches** (optional) — for risky or long-lived work,
  branch off `dev` (`feature/<short-name>`), iterate, merge back to
  `dev`. Not required for solo work.

## Versioning

Semantic Versioning, three-component (`MAJOR.MINOR.PATCH`):

- **MAJOR** — incompatible config / hook contract / CLI flag
  changes. Anything that requires the user to edit
  `config/claude-hooks.json` or re-run `install.py` to keep working.
- **MINOR** — new providers, new hook handlers, new opt-in
  subsystems, new bin/ shims, new MCP tools. Default-off additions
  do not require a major bump.
- **PATCH** — bug fixes, internal refactors, documentation, test
  additions. No behavior change visible to opted-in users.

The current version lives in **two** authoritative places — keep
them in sync:

1. `pyproject.toml` → `[project] version = "X.Y.Z"`
2. `CHANGELOG.md` → top entry `## [X.Y.Z] — YYYY-MM-DD`

The CLAUDE.md status banner is informational; it lags by one
release at most. Update it during the same cut.

## Cut procedure

1. **Sanity-check `dev`**

   ```bash
   git checkout dev
   git pull --ff-only
   /root/anaconda3/envs/claude-hooks/bin/python -m pytest tests/ -q
   pytest --collect-only -q | tail -1   # record the test count
   ```

   No failing tests; collect count noted for the CHANGELOG.

2. **Merge `dev` → `main` (fast-forward when possible)**

   ```bash
   git checkout main
   git pull --ff-only
   git merge --ff-only dev || git merge --no-ff dev
   ```

   Use `--ff-only` when `main` has not diverged. If it has,
   `--no-ff` produces a single merge commit so the release boundary
   is visible in `git log`.

3. **Bump the version on `main`**

   - Edit `pyproject.toml` → new `version`.
   - Edit `CLAUDE.md` status banner (`> Status: **vX.Y.Z**`).
   - Move `## [Unreleased]` content in `CHANGELOG.md` into a new
     `## [X.Y.Z] — YYYY-MM-DD` section. Refresh the link references
     at the bottom of the file (`[Unreleased]` and `[X.Y.Z]`).

4. **Commit the release prep**

   ```bash
   git add pyproject.toml CHANGELOG.md CLAUDE.md
   git commit -m "release: vX.Y.Z"
   ```

5. **Tag and push**

   ```bash
   git tag -a vX.Y.Z -m "claude-hooks vX.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```

   The annotated tag (`-a`) is required — GitHub uses the tag
   message as the default release body when one is not supplied.

6. **Create the GitHub release**

   ```bash
   gh release create vX.Y.Z \
       --title "claude-hooks vX.Y.Z" \
       --notes-from-tag \
       --verify-tag
   ```

   Or, to use the CHANGELOG entry verbatim:

   ```bash
   gh release create vX.Y.Z \
       --title "claude-hooks vX.Y.Z" \
       --notes-file <(awk '/^## \[X\.Y\.Z\]/,/^## \[/' CHANGELOG.md | head -n -1) \
       --verify-tag
   ```

   GitHub auto-generates `Source code (zip)` and
   `Source code (tar.gz)` archives from the tag — that satisfies
   the "downloadable zip per release" requirement; no manual
   asset upload is needed.

7. **Reset the `dev` branch on top of the new `main`**

   ```bash
   git checkout dev
   git merge --ff-only main
   git push origin dev
   ```

   `dev` is now `main + (possibly a "release: vX.Y.Z" merge commit)`
   — ready for the next round of work.

## Hotfix procedure

For a fix that must ship without waiting for the next `dev` cut:

1. Branch from `main`: `git checkout -b hotfix/<short> main`.
2. Land the fix; bump PATCH (`1.0.0 → 1.0.1`) following the cut
   procedure above. Skip the `dev → main` merge step.
3. After tagging, merge the hotfix branch back into `dev` so it
   doesn't get lost:

   ```bash
   git checkout dev
   git merge --no-ff hotfix/<short>
   git push origin dev
   git branch -d hotfix/<short>
   ```

## Pre-release tags

For experimental cuts (e.g. shipping a preview of a new provider):

```
v1.1.0-rc.1
v1.1.0-beta.1
```

Use `gh release create vX.Y.Z-rc.N --prerelease ...`. Pre-releases
do not show up as the "Latest" release on GitHub.

## What NOT to do

- Do **not** tag from `dev`. Tags belong on `main`.
- Do **not** force-push `main` once a tag has been published.
  Consumers who pulled the tag will silently end up on a different
  commit graph.
- Do **not** delete a published tag/release to "redo" it; ship
  `vX.Y.(Z+1)` with the fix instead.
- Do **not** skip the CHANGELOG update — it is the contract with
  users about what changed.

## CI hooks (future)

The current process is manual. When CI lands, gate `main` on:

- `pytest tests/ -q` (full suite, conda env)
- `python -m pyflakes claude_hooks` or ruff
- A "release prep" check that `pyproject.toml`'s version matches the
  top CHANGELOG entry on tag push.
