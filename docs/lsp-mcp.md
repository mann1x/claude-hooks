# cclsp — Multi-Language LSP via MCP

`cclsp` is a Node-based MCP server that fronts any number of language
servers (LSP) and exposes their hover / go-to-definition / diagnostics
output as MCP tools. It complements the `PostToolUse` ruff hook by
giving Claude Code real-time, language-aware context on every supported
language — not just Python.

This page documents the recommended install on Linux (solidpc-style)
and Windows (pandorum-style). Once installed and registered in
`~/.claude.json`, Claude Code can call `mcp__lsp__*` tools on any file
it edits.

---

## What you get

For any file in a configured language:

- **Hover** — symbol type, signature, doc comment.
- **Go-to-definition** — file:line of the declaration.
- **Find references** — every callsite.
- **Diagnostics** — type errors, undefined symbols, lint violations.
- **Document symbols** — outline of classes / functions in a file.

These are the same primitives VSCode and JetBrains use; cclsp wraps
them as MCP tools so a model can call them between edits.

---

## Recommended language servers

| Language    | Server         | Install                                              |
|-------------|----------------|------------------------------------------------------|
| Python      | pyright        | `npm i -g pyright`                                   |
| Go          | gopls          | `go install golang.org/x/tools/gopls@latest`         |
| Rust        | rust-analyzer  | `rustup component add rust-analyzer`                 |
| C / C++     | clangd         | distro pkg (`apt install clangd`) or upstream binary |
| C#          | OmniSharp      | upstream zip (`csharp-ls` upstream is broken)        |

C# note: `dotnet tool install -g csharp-ls` fails with
`Settings file 'DotnetToolSettings.xml' was not found in the package`
on the published artifact (upstream packaging bug). Use OmniSharp
1.39.15+ binary instead — see install steps below.

---

## Install — Linux (solidpc)

```bash
# Node-based servers
npm i -g pyright cclsp

# Go server
go install golang.org/x/tools/gopls@latest
# binary lands in $GOPATH/bin (default ~/go/bin) — make sure it's on PATH

# Rust server
rustup component add rust-analyzer
# resolved via `rustup which rust-analyzer`

# C/C++ server (Debian/Ubuntu)
sudo apt install -y clangd

# C# server — manual binary install (csharp-ls upstream broken)
mkdir -p /opt/omnisharp
cd /opt/omnisharp
curl -sSL -o omnisharp.zip \
  https://github.com/OmniSharp/omnisharp-roslyn/releases/download/v1.39.15/omnisharp-linux-x64-net6.0.tar.gz
tar xzf omnisharp.zip
ln -s /opt/omnisharp/OmniSharp /usr/local/bin/omnisharp
```

Write a project-agnostic config at `/shared/config/cclsp/cclsp.json`:

```json
{
  "servers": [
    {
      "extensions": ["py", "pyi"],
      "command": ["pyright-langserver", "--stdio"],
      "rootDir": "."
    },
    {
      "extensions": ["go"],
      "command": ["/root/go/bin/gopls"],
      "rootDir": "."
    },
    {
      "extensions": ["rs"],
      "command": ["/root/.cargo/bin/rust-analyzer"],
      "rootDir": "."
    },
    {
      "extensions": ["c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx"],
      "command": ["/usr/bin/clangd", "--background-index", "--clang-tidy"],
      "rootDir": "."
    },
    {
      "extensions": ["cs"],
      "command": ["/usr/local/bin/omnisharp", "-lsp"],
      "rootDir": "."
    }
  ]
}
```

Register in `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "lsp": {
      "type": "stdio",
      "command": "cclsp",
      "env": {
        "CCLSP_CONFIG_PATH": "/shared/config/cclsp/cclsp.json"
      }
    }
  }
}
```

---

## Install — Windows (pandorum)

```powershell
# Node-based servers
npm i -g pyright cclsp

# Go server (writes to %USERPROFILE%\go\bin)
go install golang.org/x/tools/gopls@latest

# Rust server
rustup component add rust-analyzer
```

clangd and OmniSharp ship as zip archives on Windows. Drop them in
`C:\tools\`:

```powershell
# clangd
Invoke-WebRequest `
  https://github.com/clangd/clangd/releases/download/22.1.0/clangd-windows-22.1.0.zip `
  -OutFile C:\tools\clangd.zip
Expand-Archive C:\tools\clangd.zip C:\tools\
# binary at C:\tools\clangd_22.1.0\bin\clangd.exe

# OmniSharp
Invoke-WebRequest `
  https://github.com/OmniSharp/omnisharp-roslyn/releases/download/v1.39.15/omnisharp-win-x64-net6.0.zip `
  -OutFile C:\tools\omnisharp.zip
Expand-Archive C:\tools\omnisharp.zip C:\tools\omnisharp\
# binary at C:\tools\omnisharp\OmniSharp.exe
```

Config at `C:\Users\<you>\.config\cclsp\cclsp.json`:

```json
{
  "servers": [
    {
      "extensions": ["py", "pyi"],
      "command": ["pyright-langserver.cmd", "--stdio"],
      "rootDir": "."
    },
    {
      "extensions": ["go"],
      "command": ["C:\\Users\\<you>\\go\\bin\\gopls.exe"],
      "rootDir": "."
    },
    {
      "extensions": ["rs"],
      "command": ["C:\\Users\\<you>\\.cargo\\bin\\rust-analyzer.exe"],
      "rootDir": "."
    },
    {
      "extensions": ["c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx"],
      "command": ["C:\\tools\\clangd_22.1.0\\bin\\clangd.exe", "--background-index", "--clang-tidy"],
      "rootDir": "."
    },
    {
      "extensions": ["cs"],
      "command": ["C:\\tools\\omnisharp\\OmniSharp.exe", "-lsp"],
      "rootDir": "."
    }
  ]
}
```

Register in `%USERPROFILE%\.claude.json`:

```json
{
  "mcpServers": {
    "lsp": {
      "type": "stdio",
      "command": "cclsp.cmd",
      "env": {
        "CCLSP_CONFIG_PATH": "C:\\Users\\<you>\\.config\\cclsp\\cclsp.json"
      }
    }
  }
}
```

---

## Smoke test

Linux:
```bash
cclsp --help    # cclsp emits "Available subcommands: setup" on Unknown args
```

Windows:
```cmd
cclsp.cmd --help
```

In a Claude Code session, `mcp__lsp__*` tools should appear after the
config is reloaded.

---

## Why cclsp instead of one MCP per language?

Each LSP runs as a child process under the cclsp parent. This means:

- **One MCP server entry** in `~/.claude.json` instead of five.
- **Lazy spawn** — language servers only start when a file in their
  configured extension is touched, so a Python-only session never
  pays the rust-analyzer startup cost.
- **Single config file** to keep in sync across machines.

The downside is cclsp itself is a Node MCP server (~80 MB
node_modules), but that one cost replaces the per-language MCP overhead.

---

## Relationship to the `PostToolUse` ruff hook

The ruff hook runs **synchronously** after every Python edit and
injects diagnostics into the next prompt as `additionalContext`. It
is the cheap, always-on layer.

cclsp is the **on-demand** layer: the model decides when to call
`mcp__lsp__hover` or `mcp__lsp__definition`. There's no synchronous
injection, so it doesn't bloat every prompt — but it also doesn't fire
unless the model thinks to use it.

For a fully synchronous, multi-language equivalent of the ruff hook,
see [PLAN-lsp-engine.md](PLAN-lsp-engine.md): a session-scoped daemon
that loads the project once, watches edits in real time, and answers
queries in single-digit milliseconds.
