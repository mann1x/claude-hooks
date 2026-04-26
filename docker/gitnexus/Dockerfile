# gitnexus runtime for hosts whose system glibc is too old for the
# upstream npm-installed binary's LadybugDB native module.
#
# LadybugDB 0.15+ requires glibc >= 2.32 and libstdc++ with
# GLIBCXX_3.4.32. Debian 11 (Proxmox VE 7) ships glibc 2.31 — incompatible.
# Debian trixie (12+) ships glibc 2.41+ which works.
#
# Build:
#   docker build -t gitnexus-local:latest -f docker/gitnexus/Dockerfile .
#
# Run via wrapper at docker/gitnexus/wrapper.sh (install as
# /usr/local/bin/gitnexus to make detection transparent):
#   sudo install -m 0755 docker/gitnexus/wrapper.sh /usr/local/bin/gitnexus
FROM node:22-trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm i -g gitnexus@latest && gitnexus --version
WORKDIR /work
ENTRYPOINT ["gitnexus"]
