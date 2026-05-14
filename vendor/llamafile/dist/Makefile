# Reproducible build recipe for the composite llamafile shipped as
# a v1.4+ GitHub Release asset.
#
# Inputs:
#   - vendor/llamafile/v$(VERSION)/llamafile-$(VERSION)   (slim binary)
#   - vendor/llamafile/v$(VERSION)/zipalign-$(VERSION)    (glue helper)
#   - vendor/llamafile/models/qwen3-embedding-0.6b.gguf   (model blob)
#
# Outputs:
#   - vendor/llamafile/dist/qwen3-embedding-0.6b-16k.llamafile
#   - vendor/llamafile/dist/SHA256SUMS.composite
#
# Typical use (release-cut step):
#   $ make -C vendor/llamafile/dist
#   $ gh release upload v1.4.0 \
#         vendor/llamafile/dist/qwen3-embedding-0.6b-16k.llamafile
#   $ git add vendor/llamafile/dist/SHA256SUMS.composite
#   $ git commit -m "release: refresh composite SHA"
#
# install.py reads the committed SHA256SUMS.composite at install time
# and verifies the downloaded asset matches; mismatch -> hard error
# with a "redownload or rebuild" breadcrumb.

VERSION         ?= 0.10.1
DIST_DIR        := $(dir $(realpath $(lastword $(MAKEFILE_LIST))))
REPO_ROOT       := $(abspath $(DIST_DIR)../../..)
VENDOR_DIR      := $(REPO_ROOT)/vendor/llamafile
SLIM            := $(VENDOR_DIR)/v$(VERSION)/llamafile-$(VERSION)
ZIPALIGN        := $(VENDOR_DIR)/v$(VERSION)/zipalign-$(VERSION)
MODELS_DIR      := $(VENDOR_DIR)/models
GGUF            := $(MODELS_DIR)/qwen3-embedding-0.6b.gguf
GGUF_NAME       := qwen3-embedding-0.6b.gguf
OUT             := $(DIST_DIR)qwen3-embedding-0.6b-16k.llamafile
SHA             := $(DIST_DIR)SHA256SUMS.composite
ARGS            := $(DIST_DIR).args

# Default args baked into the composite. Override via:
#   make CTX_SIZE=32768
PORT            ?= 38092
CTX_SIZE        ?= 16384
POOLING         ?= last

.PHONY: all clean check check-inputs fetch-slim fetch-gguf-ollama print-vars

all: $(OUT) $(SHA)

# ----- inputs ---------------------------------------------------------

# Pre-flight: confirm all inputs are in place before we waste time
# zipaligning a 877 MB binary onto a missing GGUF.
check-inputs:
	@echo "VERSION=$(VERSION)"
	@echo "SLIM   =$(SLIM)"
	@echo "ZIPALIGN=$(ZIPALIGN)"
	@echo "GGUF   =$(GGUF)"
	@test -x "$(SLIM)"     || (echo "missing: $(SLIM) — run 'make fetch-slim' or 'gh release download $(VERSION) --repo mozilla-ai/llamafile --pattern llamafile-$(VERSION)'"; exit 1)
	@test -x "$(ZIPALIGN)" || (echo "missing: $(ZIPALIGN) — fetch alongside the slim binary"; exit 1)
	@test -f "$(GGUF)"     || (echo "missing: $(GGUF) — run 'make fetch-gguf-ollama' (uses local Ollama blob) or download from HF"; exit 1)
	@head -c 4 "$(GGUF)" | grep -q "GGUF" || (echo "$(GGUF) doesn't start with GGUF magic"; exit 1)

# ----- composite build ------------------------------------------------

# zipalign needs the GGUF + .args file inside the same dir as the
# output (the zip entries become flat at the binary's archive root).
# We stage a copy rather than risk leaking symlink semantics into the
# zip entry — zipalign follows them but the resulting entry stores
# the link's _target_ name, which breaks `-m <bundled-name>` resolution
# at runtime.
$(OUT): $(SLIM) $(ZIPALIGN) $(GGUF) | check-inputs
	@echo "==> staging args + gguf in $(DIST_DIR)"
	@printf -- "%s\n" \
		"-m" "$(GGUF_NAME)" \
		"--embedding" \
		"--server" \
		"--host" "0.0.0.0" \
		"--port" "$(PORT)" \
		"--pooling" "$(POOLING)" \
		"--ctx-size" "$(CTX_SIZE)" \
		"..." > "$(ARGS)"
	@cp -L "$(GGUF)" "$(DIST_DIR)$(GGUF_NAME)"
	@echo "==> copying slim binary -> $(OUT)"
	@cp "$(SLIM)" "$(OUT)"
	@chmod +x "$(OUT)"
	@echo "==> zipaligning $(GGUF_NAME) + .args into $(OUT)"
	@cd "$(DIST_DIR)" && "$(ZIPALIGN)" -j0 \
		"$(notdir $(OUT))" "$(GGUF_NAME)" ".args"
	@rm -f "$(DIST_DIR)$(GGUF_NAME)" "$(ARGS)"
	@echo "==> built $(OUT) ($$(stat -c%s "$(OUT)") bytes)"

# ----- SHA generation -------------------------------------------------

# Re-generated on every (re)build. install.py reads this committed
# file to verify the downloaded GH Release asset.
$(SHA): $(OUT)
	@cd "$(DIST_DIR)" && sha256sum "$(notdir $(OUT))" > "$(notdir $(SHA))"
	@cat "$(SHA)"

# ----- inputs we can fetch automatically ------------------------------

fetch-slim:
	@echo "==> fetching llamafile-$(VERSION) from mozilla-ai/llamafile"
	@mkdir -p "$(VENDOR_DIR)/v$(VERSION)"
	@cd "$(VENDOR_DIR)/v$(VERSION)" && \
		gh release download $(VERSION) --repo mozilla-ai/llamafile \
			--pattern "llamafile-$(VERSION)" \
			--pattern "zipalign-$(VERSION)"
	@chmod +x "$(SLIM)" "$(ZIPALIGN)"

# Symlinks the local Ollama qwen3-embedding blob into models/. Only
# works on a host where Ollama has already pulled the model.
fetch-gguf-ollama:
	@echo "==> probing local Ollama for qwen3-embedding:0.6b"
	@mkdir -p "$(MODELS_DIR)"
	@MANIFEST="/usr/share/ollama/.ollama/models/manifests/registry.ollama.ai/library/qwen3-embedding/0.6b"; \
	 [ -f "$$MANIFEST" ] || MANIFEST="$$HOME/.ollama/models/manifests/registry.ollama.ai/library/qwen3-embedding/0.6b"; \
	 [ -f "$$MANIFEST" ] || (echo "no manifest at /usr/share/ollama or ~/.ollama — pull with 'ollama pull qwen3-embedding:0.6b' first"; exit 1); \
	 DIGEST=$$(python3 -c "import json,sys; m=json.load(open('$$MANIFEST')); print([l['digest'] for l in m['layers'] if l['mediaType']=='application/vnd.ollama.image.model'][0].replace('sha256:',''))"); \
	 BLOB_BASE=$$(dirname $$(dirname "$$MANIFEST"))/../../../blobs; \
	 BLOB="$$BLOB_BASE/sha256-$$DIGEST"; \
	 [ -f "$$BLOB" ] || (echo "manifest references missing blob $$BLOB"; exit 1); \
	 ln -sf "$$BLOB" "$(GGUF)"; \
	 echo "==> symlinked $$BLOB -> $(GGUF)"

# ----- verification ---------------------------------------------------

check: $(OUT)
	@echo "==> sha256 ..."
	@cd "$(DIST_DIR)" && sha256sum -c "$(notdir $(SHA))"
	@echo "==> boot probe (5s) ..."
	@"$(OUT)" --version

# ----- cleanup --------------------------------------------------------

clean:
	@rm -f "$(OUT)" "$(SHA)" "$(DIST_DIR)$(GGUF_NAME)" "$(ARGS)"

# ----- diagnostic -----------------------------------------------------

print-vars:
	@echo "VERSION   = $(VERSION)"
	@echo "DIST_DIR  = $(DIST_DIR)"
	@echo "REPO_ROOT = $(REPO_ROOT)"
	@echo "SLIM      = $(SLIM)"
	@echo "ZIPALIGN  = $(ZIPALIGN)"
	@echo "GGUF      = $(GGUF)"
	@echo "OUT       = $(OUT)"
	@echo "SHA       = $(SHA)"
	@echo "PORT      = $(PORT)"
	@echo "CTX_SIZE  = $(CTX_SIZE)"
	@echo "POOLING   = $(POOLING)"
