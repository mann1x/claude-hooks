# opencoti-llamafile c14 r19 — embedder inputs

Inputs for the opencoti embedder composite (ch-33), from
[ManniX-ITA/opencoti-llamafile](https://huggingface.co/ManniX-ITA/opencoti-llamafile)
release `llamafile-v0.10.5+opencoti.c14-r19`:

| file | what |
|---|---|
| `components/engine/2611262234001/opencoti-llamafile-0.10.5-c14-bare.llamafile` | the engine (APE: Linux, Windows, macOS; x86_64 + aarch64 CPU), no GPU libraries |
| `components/vulkan/2611270025004/ggml-vulkan-x86_64.so` | Vulkan backend, Linux x86_64 (glibc ≥ 2.27) |
| `components/vulkan/2611270025004/ggml-vulkan-win-x86_64.dll` | Vulkan backend, Windows x86_64 |

`SHA256SUMS` pins them (copied from the release's own `SHA256SUMS`).
Only the README and `SHA256SUMS` are committed; the binaries are fetched:

```bash
make -C vendor/llamafile/dist fetch-opencoti opencoti
# -> vendor/llamafile/dist/qwen3-embedding-0.6b-16k-opencoti-c14-r19.llamafile
```

The composite puts the two Vulkan libraries at the zip root as
`ggml-vulkan.so` / `ggml-vulkan.dll` (the names the engine looks for) next
to the GGUF and an `.args` that names no backend. The daemon chooses the
backend on each spawn: `docs/llamafile-integration.md`, "Backend".

Known for this release: on a Renoir iGPU (RADV) the r19 Vulkan library
decodes 1–4 % slower than r18's (opencoti bug-4393).
