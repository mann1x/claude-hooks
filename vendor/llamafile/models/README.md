# vendor/llamafile/models

Raw GGUF weights used as input to the composite `.llamafile` builds
in `../dist/`. **This directory is gitignored** — the model files
are 100s of MB to several GB each, sourced from external registries
(Ollama, HuggingFace) at build time.

## Populating

### From Ollama (preferred when available)

Ollama's model registry stores raw GGUF blobs under content-addressed
filenames. They're directly usable as llamafile input — no
conversion. Look up the digest in the manifest, then copy/symlink
the blob:

```bash
# Inspect the manifest
cat /usr/share/ollama/.ollama/models/manifests/registry.ollama.ai/library/qwen3-embedding/0.6b \
    | jq .

# Symlink the GGUF blob into models/
ln -s /usr/share/ollama/.ollama/models/blobs/sha256-<digest> \
      vendor/llamafile/models/qwen3-embedding-0.6b.gguf
```

Verify the magic bytes:

```bash
head -c 4 vendor/llamafile/models/qwen3-embedding-0.6b.gguf
# → GGUF
```

### From HuggingFace (fallback)

```bash
huggingface-cli download <owner>/<repo> <file>.gguf \
    --local-dir vendor/llamafile/models/
```

## Currently expected models

| File | Origin | Quant | Dim | Ctx | Used by |
|---|---|---|---|---|---|
| `qwen3-embedding-0.6b.gguf` | Ollama `qwen3-embedding:0.6b` blob | Q8_0 | 1024 | 16k (used at 16k) | `dist/qwen3-embedding-0.6b-16k.llamafile` |

Update this table whenever a new entry is added to `../dist/`.
