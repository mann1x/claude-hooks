---
id: ch-3
title: "Status-line mail badge: count waiting ack notes"
status: done
priority: M
area: mailbox.statusline
links: {commits: [8f9ff60]}
created: 2026-10-02T18:30:03Z
updated: 2026-10-02T18:33:08Z
sessions: [0a7a5bf4]
---

## Description
The badge counts only unread inbox messages (inbox_count). An ack note on a message this session sent (ack_body set, receipt_read_at NULL) is announced at the next prompt but never lights the badge, so an idle session shows nothing. Show it as a second number: 📬 2 ↩1.

## Acceptance
- [x] badge shows waiting ack notes
- [x] old cache files still read
- [x] tests + docs/statusline.md
- [x] deployed on both hosts

## Log
- 2026-10-02 18:33Z [0a7a5bf4] active → done: 8f9ff60 deployed on solidpc and pandorum; 213 mailbox + status-line tests pass on Windows
- 2026-10-02 18:33Z [0a7a5bf4] linked commit 8f9ff60
- 2026-10-02 18:33Z [0a7a5bf4] updated acceptance ✓4
- 2026-10-02 18:31Z [0a7a5bf4] updated acceptance ✓1 ✓2 ✓3: receipt_count() + mail_counts(); live check on the real mailbox showed '📬 ↩1' / 'ack:1' after an ack and nothing for a bare read (test message 637 removed)
- 2026-10-02 18:30Z [0a7a5bf4] created
