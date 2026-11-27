#!/bin/bash
# Compose an opencoti embedder llamafile (ch-33): bare engine + the GGUF +
# a backend-neutral .args + the given GPU libraries at the zip root.
#
# usage: oc_compose.sh SUMS OUT BARE ZIPALIGN GGUF STAGE PORT CTX POOLING SRC:ZIPNAME...
#
# SUMS is the pinned SHA256SUMS (paths relative to its directory); the
# bare engine and every library are checked against it before use.
#
# The .args name no backend: an engine started without --gpu picks a GPU
# itself, so claude-hooks passes one on every spawn
# (claude_hooks/llamafile_backend.py). Staged files are removed by name.
set -euo pipefail
sums=$1 out=$2 bare=$3 zipalign=$4 gguf=$5 stage=$6 port=$7 ctx=$8 pooling=$9
shift 9
sums_dir=$(cd "$(dirname "$sums")" && pwd)
verify() {  # file
    local rel=${1#"$sums_dir"/} want got
    want=$(awk -v f="$rel" '$2 == f {print $1}' "$sums")
    [ -n "$want" ] || { echo "not pinned in $sums: $rel" >&2; exit 1; }
    got=$(sha256sum "$1" | cut -d' ' -f1)
    [ "$want" = "$got" ] || { echo "checksum mismatch: $rel" >&2; exit 1; }
}
verify "$bare"
for pair in "$@"; do verify "${pair%%:*}"; done
gguf_name=$(basename "$gguf")
mkdir -p "$stage"
cd "$stage"
staged=(".args" "$gguf_name" "out.llamafile")
printf -- "%s\n" -m "$gguf_name" --embedding --server --host 0.0.0.0 \
    --port "$port" --pooling "$pooling" --ctx-size "$ctx" ... > .args
if grep -q -E -- '^(--gpu|-ngl|--n-gpu-layers|-dev|--device)$' .args; then
    echo "refusing: .args names a backend" >&2
    exit 1
fi
cp -L "$gguf" "$gguf_name"
names=()
for pair in "$@"; do
    src=${pair%%:*} name=${pair##*:}
    cp "$src" "$name"
    names+=("$name")
    staged+=("$name")
done
cp "$bare" out.llamafile
chmod +x out.llamafile
"$zipalign" -j0 out.llamafile "$gguf_name" .args "${names[@]}"
mv out.llamafile "$out"
for f in "${staged[@]}"; do
    rm -f -- "$f"
done
echo "==> built $out ($(stat -c%s "$out") bytes)"
(cd "$(dirname "$out")" && sha256sum "$(basename "$out")")
