# llamafile v0.10.1

Vendored from https://github.com/mozilla-ai/llamafile/releases/tag/0.10.1
on 2026-05-14.

| Field | Value |
|---|---|
| Tag | `0.10.1` |
| Published | 2026-05-01 by @aittalam |
| llama.cpp submodule | `5e9c63546` |
| License | Apache-2.0 (see `LICENSE.upstream`) |

## Files

| File | Purpose | Size |
|---|---|---|
| `llamafile-0.10.1` | Fat (all-platforms, all-arches) llamafile binary | 837 MB |
| `zipalign-0.10.1` | Glues a GGUF + arg-file into a llamafile | 832 KB |
| `llamafile-0.10.1.zip` | Source bundle (build scripts, headers) | 236 MB |
| `LICENSE.upstream` | Apache-2.0 license file from upstream | 11 KB |
| `SHA256SUMS` | Checksum manifest for the three binaries | — |

The two `.10.1` binaries and the zip are **gitignored** — too large
for the repo. They are reconstructable with:

```bash
gh release download 0.10.1 --repo mozilla-ai/llamafile \
    --pattern "llamafile-0.10.1" \
    --pattern "zipalign-0.10.1" \
    --pattern "llamafile-0.10.1.zip"
chmod +x llamafile-0.10.1 zipalign-0.10.1
sha256sum -c SHA256SUMS
```

## What's new in 0.10.1 vs 0.10.0

From the upstream release notes:

- **Vulkan dylib support** — new `.so` / `.dll` build paths for
  CUDA / ROCm / Vulkan on Windows.
- **`tinyblasStrsmBatched` kernel** — perf win on certain matmuls.
- **GGUF Q5_1 crash on aarch64 CPU** — fixed.
- **GLIBCXX_3.4.32 missing** on older Ubuntu/Debian — fixed.
- **llama.cpp bumped to `5e9c63546`** — pulls in support for
  gemma-4, bonsai, Qwen3.6, and the new in-llama.cpp **internal
  tools API** for agentic flows (tool-call schema parsing inside
  the llama.cpp runtime rather than requiring an external proxy).
- New Windows build scripts for CUDA / ROCm / Vulkan dylibs.
- Migrated docs from MkDocs/GitHub Pages → GitBook.

## Smoke test

```bash
$ ./llamafile-0.10.1 --version
llamafile v0.10.1
```
