#!/bin/bash
# Build a private Mesa RADV Vulkan driver (ACO only, no LLVM, no GL, no
# window system) plus the libdrm it needs, for the embedder's Vulkan
# backend on a host whose system Mesa is too old (ch-33).
#
# Why: on solidpc (Debian bullseye, Mesa 20.3.5) opencoti-llamafile
# refuses RADV, and with OPENCOTI_VK_ALLOW_OLD_DRIVER=1 it returns wrong
# embeddings with no error (cosine 0.38 against the CPU). bullseye and
# bullseye-backports carry nothing newer. Mesa 25.2.8 gives the CPU's
# vectors (min cosine 0.99905 over the parity set).
#
# Nothing system-wide changes: the driver is used only by the embedder,
# through `embedding.vulkan_icd` = <prefix>/share/vulkan/icd.d/radeon_icd.x86_64.json.
# The driver carries an RPATH to <prefix>, so it loads this libdrm, not
# the system's.
#
# usage: scripts/build_private_radv.sh [ROOT]    (default /opt/claude-hooks-radv)
# needs: gcc/g++, meson >= 1.4, ninja, python3 mako + pyyaml + packaging,
#        glslangValidator, bison, flex, libelf/zstd/expat/zlib headers.
set -euo pipefail

MESA=25.2.8
MESA_SHA=097842f3e49d996868b38688db87b006f7d4541e93ce86d2f341d8b3e7be7c93   # docs.mesa3d.org/relnotes/25.2.8
DRM=2.4.124
DRM_SHA=ac36293f61ca4aafaf4b16a2a7afff312aa4f5c37c9fbd797de9e3c0863ca379    # = Debian libdrm_2.4.124.orig.tar.xz

ROOT=${1:-/opt/claude-hooks-radv}
P=$ROOT/$MESA
S=$ROOT/src
mkdir -p "$S"
cd "$S"

fetch() {  # url sha
    local f=${1##*/}
    [ -f "$f" ] || curl -sSfLO "$1"
    echo "$2  $f" | sha256sum -c --quiet || { echo "checksum mismatch: $f" >&2; exit 1; }
}
fetch "https://archive.mesa3d.org/mesa-$MESA.tar.xz" "$MESA_SHA"
fetch "https://dri.freedesktop.org/libdrm/libdrm-$DRM.tar.xz" "$DRM_SHA"

export PKG_CONFIG_PATH=$P/lib/x86_64-linux-gnu/pkgconfig:$P/lib/pkgconfig
export LDFLAGS="-Wl,-rpath,$P/lib/x86_64-linux-gnu -Wl,--disable-new-dtags"
JOBS=${JOBS:-$(( $(nproc) > 2 ? $(nproc) - 2 : 1 ))}

[ -d "libdrm-$DRM" ] || tar xf "libdrm-$DRM.tar.xz"
(cd "libdrm-$DRM" &&
 meson setup --wipe build --prefix="$P" --buildtype=release -Damdgpu=enabled \
   -Dradeon=disabled -Dintel=disabled -Dnouveau=disabled -Dvmwgfx=disabled \
   -Domap=disabled -Dexynos=disabled -Dfreedreno=disabled -Dtegra=disabled \
   -Dvc4=disabled -Detnaviv=disabled -Dcairo-tests=disabled -Dman-pages=disabled \
   -Dvalgrind=disabled -Dtests=false -Dudev=false >/dev/null &&
 nice -n 10 ninja -C build -j"$JOBS" install >/dev/null)

[ -d "mesa-$MESA" ] || tar xf "mesa-$MESA.tar.xz"
(cd "mesa-$MESA" &&
 meson setup --wipe build --prefix="$P" --buildtype=release -Db_ndebug=true \
   -Dplatforms= -Dgallium-drivers= -Dvulkan-drivers=amd -Dllvm=disabled \
   -Damd-use-llvm=false -Dglx=disabled -Degl=disabled -Dgbm=disabled \
   -Dgles1=disabled -Dgles2=disabled -Dopengl=false -Dglvnd=disabled \
   -Dvulkan-layers= -Dtools= -Dbuild-tests=false -Dvalgrind=disabled \
   -Dlibunwind=disabled -Dlmsensors=disabled -Dvideo-codecs= >/dev/null &&
 nice -n 10 ninja -C build -j"$JOBS" install >/dev/null)

ICD=$P/share/vulkan/icd.d/radeon_icd.x86_64.json
ldd "$P/lib/x86_64-linux-gnu/libvulkan_radeon.so" | grep -q "$P/lib/x86_64-linux-gnu/libdrm_amdgpu" \
    || { echo "libvulkan_radeon.so does not load the private libdrm" >&2; exit 1; }
echo "built: $ICD"
echo "set embedding.vulkan_icd to it (install.py asks), with embedding.mode = vulkan"
