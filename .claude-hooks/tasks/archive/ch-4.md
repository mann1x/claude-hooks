---
id: ch-4
title: "Mailbox alias follows the session name (/rename); a second live session in a folder gets <alias>-N"
status: done
priority: H
area: mailbox.identity
tags: [mailbox, alias, rename]
created: 2026-10-03T09:44:22Z
updated: 2026-10-03T10:05:38Z
sessions: [0a7a5bf4]
---

## Description
A new opencoti session (/rename opencoti-mac) registered as `opencoti` (folder name), evicted the running session 9b4cc035 and showed its mail. The mailbox never read Claude Code's session name (transcript `custom-title` / history `/rename`). Fix: on every mailbox operation (hooks, MCP tools, status line) resolve the alias from the session name first, then mailbox.toml, then folder; claim it only from a dead holder or the same Claude Code process (pid + start time); otherwise `<alias>-2`, `-3`…; a rename moves unread mail sent to the old alias while this session held it; SessionStart/rename notes state the alias.

## Acceptance
- [ ] session name from transcript custom-title, history /rename fallback
- [ ] registry rows carry client pid + start time (both dialects, migration)
- [ ] live holder -> numbered suffix; dead holder or same process -> take over
- [ ] MCP tools re-resolve identity on every call, incl. stale sid after /clear
- [ ] rename moves unread mail received while holding the old alias
- [ ] tests on both dialects; full suite on solidpc + pandorum
- [ ] deployed on both hosts

## Log
- 2026-10-03 10:05Z [0a7a5bf4] active → done: 61ef9a5 on dev, pushed; deployed solidpc + pandorum (DEPLOY OK both). Full suite: solidpc 6961 passed, pandorum 6830 passed; Postgres claim/migration test passed in a throwaway schema. Windows Toolhelp walk verified live (claude.exe 41168 found). Filed anthropics/claude-code#99200 for the /rename stall. Running sessions' MCP servers keep old code until those sessions restart; hooks are on the new code now.
- 2026-10-03 09:44Z [0a7a5bf4] created
