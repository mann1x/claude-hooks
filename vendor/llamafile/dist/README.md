# vendor/llamafile/dist

Composite `.llamafile` artifacts: the llamafile binary from
`../v0.10.1/` + a GGUF from `../models/` + a baked-in args file,
all zipaligned into a single executable. **This directory is
gitignored** — outputs are reconstructable from committed inputs.

## What's a "composite llamafile"

A llamafile is a Cosmopolitan-Libc executable plus a zipfile
appended. `zipalign` is the helper that adds files to that
appended zipfile. Concretely:

```bash
cp ../v0.10.1/llamafile-0.10.1 qwen3-embedding-0.6b-16k.llamafile
chmod +x qwen3-embedding-0.6b-16k.llamafile

# Append the GGUF + args file as zipfile entries inside the binary
../v0.10.1/zipalign-0.10.1 -j0 qwen3-embedding-0.6b-16k.llamafile \
    ../models/qwen3-embedding-0.6b.gguf \
    .args
```

`.args` is a tiny file holding the default CLI arguments — these
become the **defaults** the resulting llamafile boots with, but the
user can still override them by passing CLI flags after the binary
name.

For an embedding server the relevant `.args` looks like:

```
-m
qwen3-embedding-0.6b.gguf
--embedding
--ctx-size
16384
--pooling
last
--server
--port
8080
--host
0.0.0.0
...
```

`-m qwen3-embedding-0.6b.gguf` tells llamafile to look for the
model **inside its own zip**, not on disk — that's the magic that
makes the result a single self-contained file.

## Current artifacts (intended layout)

| File | Model | Mode | Ctx | Port (default) |
|---|---|---|---|---|
| `qwen3-embedding-0.6b-16k.llamafile` | qwen3-embedding:0.6b | embedding-only | 16k | 8080 |

Bench / parity results live alongside in `docs/llamafile-embedding-parity.md`.
