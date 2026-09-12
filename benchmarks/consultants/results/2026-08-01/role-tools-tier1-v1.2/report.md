# M-B role-tools bench — Tier 1 (detection)

bench_version=1.0  trials=36

| metric | untooled | tooled |
|---|---|---|
| recall (planted falsehoods caught) | 6.7% | 60.0% |
| precision (flags that were real) | 100.0% | 100.0% |
| false positives | 0 | 0 |
| tool calls | 0 | 32 |
| completion tokens | 1207 | 1900 |
| wall (s) | 55.58 | 71.01 |

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
| easy-01-fabricated-file | tooled | 1 | 0 | 0 | 2 |
| easy-01-fabricated-file | tooled | 1 | 0 | 0 | 3 |
| easy-01-fabricated-file | tooled | 1 | 0 | 0 | 3 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 1 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 1 |
| easy-02-wrong-constant | tooled | 1 | 0 | 0 | 1 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 2 |
| hard-01-mixed | tooled | 0 | 1 | 0 | 2 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 4 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 1 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 1 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 1 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | untooled | 1 | 0 | 0 | 0 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 1 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 1 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 2 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 2 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 2 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 2 |
