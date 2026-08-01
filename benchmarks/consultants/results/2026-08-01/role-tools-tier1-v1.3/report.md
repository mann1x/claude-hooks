# M-B role-tools bench — Tier 1 (detection)

bench_version=1.0  trials=72

| metric | untooled | tooled |
|---|---|---|
| recall (planted falsehoods caught) | 0.0% | 100.0% |
| precision (flags that were real) | n/a | 100.0% |
| false positives | 0 | 0 |
| silent fixes (looked, knew, didn't say) | 0 | 0 |
| trials reporting CORRECTIONS | 0 | 13 |
| tool calls | 0 | 70 |
| completion tokens | 2426 | 3916 |
| wall (s) | 86.24 | 163.26 |

Recall is a **lower bound**. A catch is scored two ways: the
verdict names the fabricated token near a doubt word, or names
it inside a `CORRECTIONS:` block (where naming a claim *is*
disputing it). A critic that describes the problem without
naming the token still reads as a miss. The block rule counts
symmetrically — a *true* claim quoted inside it scores as a
false positive — so it is not a one-way loosening.
Read transcripts before acting on a close call.

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
| easy-02-wrong-constant | tooled | 1 | 0 | 0 | 0 | y | 1 |
| easy-02-wrong-constant | tooled | 1 | 0 | 0 | 0 | y | 1 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 2 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 2 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 4 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | tooled | 1 | 0 | 0 | 0 | y | 2 |
| hard-02-line-drift | tooled | 1 | 0 | 0 | 0 | y | 2 |
| hard-02-line-drift | tooled | 1 | 0 | 0 | 0 | y | 2 |
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
| easy-02-wrong-constant | tooled | 1 | 0 | 0 | 0 | y | 1 |
| easy-02-wrong-constant | tooled | 1 | 0 | 0 | 0 | y | 1 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 4 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | - | 3 |
| hard-01-mixed | tooled | 1 | 0 | 0 | 0 | y | 2 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | untooled | 0 | 1 | 0 | 0 | - | 0 |
| hard-02-line-drift | tooled | 1 | 0 | 0 | 0 | y | 1 |
| hard-02-line-drift | tooled | 1 | 0 | 0 | 0 | y | 2 |
| hard-02-line-drift | tooled | 1 | 0 | 0 | 0 | y | 2 |
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
