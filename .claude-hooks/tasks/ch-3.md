---
id: ch-3
title: "Status-line mail badge: count waiting ack notes"
status: active
priority: M
area: mailbox.statusline
created: 2026-10-02T18:30:03Z
updated: 2026-10-02T18:31:43Z
sessions: [0a7a5bf4]
---

## Description
The badge counts only unread inbox messages (inbox_count). An ack note on a message this session sent (ack_body set, receipt_read_at NULL) is announced at the next prompt but never lights the badge, so an idle session shows nothing. Show it as a second number: 📬 2 ↩1.

## Acceptance
- [x] badge shows waiting ack notes
- [x] old cache files still read
- [x] tests + docs/statusline.md
- [ ] deployed on both hosts

## Log
- 2026-10-02 18:31Z [0a7a5bf4] updated acceptance ✓1 ✓2 ✓3: receipt_count() + mail_counts(); live check on the real mailbox showed '📬 ↩1' / 'ack:1' after an ack and nothing for a bare read (test message 637 removed)
- 2026-10-02 18:30Z [0a7a5bf4] created
