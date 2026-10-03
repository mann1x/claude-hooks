---
id: ch-2
title: "Cut the release that ships task tracking and the cloud mailbox relay"
status: done
priority: M
area: release
depends: [ch-1]
created: 2026-10-02T16:37:47Z
updated: 2026-10-02T18:57:56Z
sessions: [0a7a5bf4]
---

## Description
Both features sit under [Unreleased] on dev. Needs the user's go-ahead: version bump in pyproject + CHANGELOG + CLAUDE.md, pandorum smoke before the dev→main merge and tag.

## Log
- 2026-10-02 18:57Z [0a7a5bf4] active → done: v1.20.0 tagged at 9e5b820 and published (github.com/mann1x/claude-hooks/releases/tag/v1.20.0); full suites clean on solidpc (6923) and pandorum (6801) after fixing a host-pinning test fixture (71c03a7); deployed on both hosts
- 2026-10-02 18:34Z [0a7a5bf4] waiting → active: user: "cut a new release and deploy to solidpc and pandorum"
- 2026-10-02 16:37Z [0a7a5bf4] pending → waiting: waiting for the user's go-ahead to release
- 2026-10-02 16:37Z [0a7a5bf4] created
