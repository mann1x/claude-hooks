# gitnexus Docker wrapper

A drop-in replacement for the npm-installed `gitnexus` binary, for
hosts whose system libraries are too old to load LadybugDB's native
module.

## When you need this

Run `gitnexus --version` on the host. If it crashes with one of these:

- `Error: /lib/x86_64-linux-gnu/libc.so.6: version 'GLIBC_2.32' not found`
- `Error: /lib/x86_64-linux-gnu/libstdc++.so.6: version 'GLIBCXX_3.4.32' not found`

…then your host's libraries are older than what LadybugDB needs (it ships
prebuilt for Debian trixie / Ubuntu 24.04+). The wrapper here gives you
a working `gitnexus` by running it inside a Debian-trixie container.

Confirmed needed on:

- Debian 11 (Proxmox VE 7) — glibc 2.31

Not needed on Debian 12+, Ubuntu 22.04+, Windows, or macOS.

## Install

```bash
# Build the image (~150 MB)
docker build -t gitnexus-local:latest -f docker/gitnexus/Dockerfile .

# Move the broken upstream binary aside
sudo mv /usr/local/bin/gitnexus /usr/local/bin/gitnexus.npm-broken-glibc

# Drop the wrapper in
sudo install -m 0755 docker/gitnexus/wrapper.sh /usr/local/bin/gitnexus

# Verify
gitnexus --version    # 1.6.3
gitnexus list         # empty on first run
```

claude-hooks's `companion_integration` detects gitnexus via `which gitnexus`,
so once the wrapper is on PATH, all the SessionStart hints + Stop-hook
reindex triggers pick it up automatically.

## How it works

The wrapper is one line of bash:

```bash
exec docker run --rm \
    -v /shared/config/gitnexus:/root/.gitnexus \
    -v /srv:/srv \
    -v /shared:/shared \
    -v /opt:/opt \
    -w "$(pwd -P)" \
    gitnexus-local:latest "$@"
```

Each `gitnexus <subcommand>` spawns an ephemeral container, runs the
subcommand, then exits. **No long-lived containers** — `--rm` cleans
up after each invocation. Persistence comes from:

- **`/shared/config/gitnexus/`** → mounted to `/root/.gitnexus/` in the
  container. Contains the global registry (`registry.json`) plus
  LadybugDB extension caches.
- **Per-repo `.gitnexus/`** → lives on the host filesystem inside each
  indexed repo. The `-v /srv:/srv` mount means containerized writes to
  `/srv/.../<repo>/.gitnexus/` land directly on the host.

## Tuning the mounts

If your repos live somewhere other than `/srv`, `/shared`, or `/opt`,
either edit the wrapper to add the right `-v` flags or set
`GITNEXUS_CONFIG_DIR` for the registry. Keep absolute paths the same
inside and outside the container so `gitnexus analyze /path/to/repo`
works without translation.

## Multi-session MCP

The wrapper supports `gitnexus mcp` (stdio MCP server) the same way —
each Claude Code session that opens the gitnexus MCP gets its own
ephemeral container.

For the experimental shared-host HTTP MCP (`gitnexus serve`), spin up
a detached container manually:

```bash
docker run -d --name gitnexus-host \
    -v /shared/config/gitnexus:/root/.gitnexus \
    -v /srv:/srv \
    -p 4747:4747 \
    gitnexus-local:latest serve
```

(claude-hooks doesn't currently wire this up — stdio is the default.)
