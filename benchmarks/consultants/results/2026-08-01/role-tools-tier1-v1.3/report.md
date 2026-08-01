# M-B role-tools bench — Tier 1 (detection)

bench_version=1.0  trials=36

| metric | untooled | tooled |
|---|---|---|
| recall (planted falsehoods caught) | 0.0% | 66.7% |
| precision (flags that were real) | n/a | 100.0% |
| false positives | 0 | 0 |
| silent fixes (looked, knew, didn't say) | 0 | 0 |
| trials reporting CORRECTIONS | 0 | 6 |
| tool calls | 0 | 35 |
| completion tokens | 1180 | 2059 |
| wall (s) | 41.97 | 75.58 |

Recall here is a **lower bound**: the oracle scores a catch only
when the verdict names the fabricated token near a doubt word, so
a critic that describes the problem without naming it reads as a
miss. Read transcripts before acting on a close call.

## Per question

| question | arm | caught | missed | false pos | silent | corr | tools |
|---|---|---|---|---|---|---|---|
| easy-01-fabricated-file | untooled | 0 | 1 | 0 | 0 | - | 0 |
| easy-01-fabricated-file | untooled | 0 | 1 | 0 | 0 | - | 0 |
| easy-01-fabricated-file | untooled | 0 | 1 | 0 | 0 | - | 0 |
| easy-01-fabricated-file | tooled | 1 | 0 | 0 | 0 | - | 3 |
| easy-01-fabricated-file | tooled | 1 | 0 | 0 | 0 | - | 3 |
| easy-01-fabricated-file | tooled | 1 | 0 | 0 | 0 | - | 3 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 | - | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 | - | 0 |
| easy-02-wrong-constant | untooled | 0 | 1 | 0 | 0 | - | 0 |
| easy-02-wrong-constant | tooled | 1 | 0 | 0 | 0 | y | 1 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 0 | y | 1 |
| easy-02-wrong-constant | tooled | 0 | 1 | 0 | 0 | y | 1 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 2 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 2 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 4 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 0 | y | 2 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 0 | y | 2 |
| hard-02-line-drift | tooled | 0 | 1 | 0 | 0 | y | 2 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 | - | 0 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 | - | 0 |
| medium-01-nonexistent-symbol | untooled | 0 | 1 | 0 | 0 | - | 0 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 0 | - | 1 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 0 | - | 1 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 0 | - | 1 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 | - | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 | - | 0 |
| medium-02-all-true | untooled | 0 | 0 | 0 | 0 | - | 0 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 0 | - | 2 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 0 | - | 2 |
| medium-02-all-true | tooled | 0 | 0 | 0 | 0 | - | 2 |
