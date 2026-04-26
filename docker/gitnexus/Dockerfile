# gitnexus runtime for hosts whose system glibc is too old for the
# upstream npm-installed binary's LadybugDB native module.
#
# LadybugDB 0.15+ requires glibc >= 2.32 and libstdc++ with
# GLIBCXX_3.4.32. Debian 11 (Proxmox VE 7) ships glibc 2.31 — incompatible.
# Debian trixie (12+) ships glibc 2.41+ which works.
#
# Default build (latest npm-published gitnexus):
#   docker build -t gitnexus-local:latest -f docker/gitnexus/Dockerfile .
#
# Build with a forked tarball (e.g., to pick up
# https://github.com/abhigyanpatwari/GitNexus/pull/1083 before it ships
# in an npm release):
#
#   # Place gitnexus-X.Y.Z.tgz next to this Dockerfile, then:
#   docker build --build-arg GITNEXUS_TARBALL=gitnexus-1.6.3.tgz \
#                -t gitnexus-local:latest \
#                -f docker/gitnexus/Dockerfile docker/gitnexus
#
# Run via wrapper at docker/gitnexus/wrapper.sh (install as
# /usr/local/bin/gitnexus to make detection transparent):
#   sudo install -m 0755 docker/gitnexus/wrapper.sh /usr/local/bin/gitnexus
FROM node:22-trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG GITNEXUS_TARBALL=
COPY *.tgz /tmp/
RUN if [ -n "$GITNEXUS_TARBALL" ] && [ -f "/tmp/$GITNEXUS_TARBALL" ]; then \
      echo "Installing forked gitnexus from /tmp/$GITNEXUS_TARBALL" && \
      npm i -g "/tmp/$GITNEXUS_TARBALL"; \
    else \
      echo "Installing latest published gitnexus from npm" && \
      npm i -g gitnexus@latest; \
    fi \
    && gitnexus --version \
    && rm -f /tmp/*.tgz
WORKDIR /work
ENTRYPOINT ["gitnexus"]
