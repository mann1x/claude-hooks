# M-B role-tools bench — Tier 1 (detection)

bench_version=1.0  trials=36

| metric | untooled | tooled |
|---|---|---|
| recall (planted falsehoods caught) | 0.0% | 6.7% |
| precision (flags that were real) | n/a | 20.0% |
| false positives | 0 | 4 |
| tool calls | 0 | 0 |
| completion tokens | 1199 | 1687 |
| wall (s) | 23.57 | 29.88 |

Recall here is a **lower bound**: the oracle scores a catch only
when the verdict names the fabricated token near a doubt word, so
a critic that describes the problem without naming it reads as a
miss. Read transcripts before acting on a close call.

## Per question

| question | arm | caught | missed | false pos | tools |
|---|---|---|---|---|---|
| easy-01-fabricated-file | untooled | 0 | 1 | 0 | 0 |
| easy-01-fabricated-file | untooled | 0 | 1 | 0 | 0 |
| easy-01-fabricated-file | untooled | 0 | 1 | 0 | 0 |
| easy-01-fabricated-file | tooled | 0 | 1 | 0 | 0 |
| easy-01-fabricated-file | tooled | 0 | 1 | 0 | 0 |
| easy-01-fabricated-file | tooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | tooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | tooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | tooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | tooled | 1 | 0 | 2 | 0 |
| hard-02-line-drift | tooled | 0 | 1 | 2 | 0 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | tooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | tooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | tooled | 0 | 1 | 0 | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 0 |
