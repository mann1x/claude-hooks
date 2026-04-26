#!/usr/bin/env bash
# gitnexus Docker wrapper — drop-in replacement for the npm-installed
# binary on hosts whose glibc/libstdc++ can't load LadybugDB's native
# module (Debian <= 11 / Proxmox VE 7 / similar older Linux).
#
# Install:
#   sudo install -m 0755 docker/gitnexus/wrapper.sh /usr/local/bin/gitnexus
#   # First run will use the gitnexus-local:latest image — build it via
#   # the Dockerfile next to this script.
#
# Persistence:
#   /shared/config/gitnexus is mounted to /root/.gitnexus inside the
#   container so the global registry + cache survive container exits.
#   Per-repo .gitnexus/ directories live inside each indexed repo on
#   the host filesystem (bind-mounted via the /srv and /shared mounts),
#   so they persist there too.
#
# Path mounts:
#   /srv, /shared, /opt are mounted read-write so absolute paths to repos
#   under those trees Just Work without --volume-fu. Adjust if your repos
#   live elsewhere.

set -euo pipefail

CONFIG_DIR="${GITNEXUS_CONFIG_DIR:-/shared/config/gitnexus}"
mkdir -p "$CONFIG_DIR"

exec docker run --rm \
    -v "$CONFIG_DIR:/root/.gitnexus" \
    -v /srv:/srv \
    -v /shared:/shared \
    -v /opt:/opt \
    -w "$(pwd -P)" \
    gitnexus-local:latest "$@"
