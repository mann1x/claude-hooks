# vendor/llamafile

Vendored copies of [mozilla-ai/llamafile](https://github.com/mozilla-ai/llamafile)
— the "distribute and run LLMs as a single executable" tooling Mozilla
inherited from the Mozilla-Ocho org. We pin a specific upstream
release here and build our own composite `.llamafile` artifacts on top
of that binary (the GGUF weights + a baked-in args file +
[zipalign](https://github.com/mozilla-ai/llamafile/blob/main/docs/zipalign.md)-glued
into one executable).

## Why vendor at all

llamafile artifacts are **stable, immutable, and tied to a specific
binary version + GGUF weights + arg-file**. Pulling the version into
the tree means:

1. **Reproducibility.** A user `git clone`-ing the repo and running
   `make -C vendor/llamafile dist` gets bit-identical output every
   time — same llamafile binary, same model bytes, same default
   args. Bug reports become reproducible.
2. **Offline builds.** No GitHub release or HuggingFace pull at
   build time once the source artifacts are downloaded once.
3. **Audit boundary.** Every llamafile binary that ships with
   claude-hooks corresponds to exactly one upstream release we've
   reviewed. No "what version is the user running" ambiguity.

## Layout

```
vendor/llamafile/
├── README.md             # this file
├── v0.10.1/              # one dir per pinned upstream release
│   ├── llamafile-0.10.1  # the fat (all-platforms) binary
│   ├── zipalign-0.10.1   # helper used to glue GGUF + args into a llamafile
│   ├── llamafile-0.10.1.zip   # source archive (build scripts, headers)
│   ├── LICENSE.upstream  # Apache-2.0 from mozilla-ai/llamafile@0.10.1
│   └── SHA256SUMS        # checksum manifest for the three binaries above
├── models/               # raw GGUF weights (gitignored, reused across builds)
│   └── README.md         # how to populate this dir
└── dist/                 # composite .llamafile artifacts (gitignored)
    └── README.md
```

The version directory (`v0.10.1/`) is the **canonical record** of
which upstream release we ship. The binaries inside it are
gitignored — too large for the repo — but the `SHA256SUMS` and
`LICENSE.upstream` files are committed so we have a verifiable
audit trail without bloating the tree.

## Re-creating the vendored binaries on a fresh checkout

```bash
cd vendor/llamafile/v0.10.1
gh release download 0.10.1 --repo mozilla-ai/llamafile \
    --pattern "llamafile-0.10.1" \
    --pattern "zipalign-0.10.1" \
    --pattern "llamafile-0.10.1.zip"
chmod +x llamafile-0.10.1 zipalign-0.10.1
sha256sum -c SHA256SUMS
```

If the checksum line fails, **do not proceed** — investigate before
running anything. The committed `SHA256SUMS` is the contract.

## Currently pinned version

| Field | Value |
|---|---|
| Tag | `0.10.1` |
| Published | 2026-05-01 |
| Upstream URL | https://github.com/mozilla-ai/llamafile/releases/tag/0.10.1 |
| llama.cpp submodule | `5e9c63546` (gemma-4 / bonsai / Qwen3.6 support, agent internal-tools API) |
| License | Apache-2.0 |
| Vendored on | 2026-05-14 |

## Adding a new pinned version

1. Create `vendor/llamafile/vX.Y.Z/`.
2. `gh release download X.Y.Z` for the three assets above.
3. `chmod +x` the binaries.
4. `sha256sum llamafile-X.Y.Z zipalign-X.Y.Z llamafile-X.Y.Z.zip > SHA256SUMS`.
5. Copy `LICENSE.upstream` from upstream (`gh api repos/mozilla-ai/llamafile/contents/LICENSE`).
6. Bump the "Currently pinned version" table above.
7. Smoke-test: `./llamafile-X.Y.Z --version`.
8. Update consumers (build scripts, dist Makefile) to point at the new dir.
9. Leave the previous version dir on disk for a release or two so we
   can A/B-bench composite artifacts; delete only once integration
   has cut over.
