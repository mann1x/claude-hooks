# M-B role-tools bench — Tier 1 (detection)

bench_version=1.0  trials=36

| metric | untooled | tooled | tooled-typed |
|---|---|---|---|
| recall (planted falsehoods caught) | n/a | 100.0% | 100.0% |
| precision (flags that were real) | n/a | 34.9% | 38.5% |
| false positives | 0 | 28 | 24 |
| silent fixes (looked, knew, didn't say) | 0 | 0 | 0 |
| trials reporting CORRECTIONS | 0 | 15 | 15 |
| tool calls | 0 | 67 | 71 |
| completion tokens | 0 | 29045 | 36029 |
| completion tokens per trial | None | 1614 | 2002 |
| prompt tokens per trial | None | 2300 | 2471 |
| wall (s) | 0 | 211.91 | 241.28 |

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
| easy-01-fabricated-file | tooled | 1 | 0 | 2 | 0 | y | 4 |
| easy-01-fabricated-file | tooled | 1 | 0 | 1 | 0 | y | 4 |
| easy-01-fabricated-file | tooled | 1 | 0 | 2 | 0 | y | 4 |
| easy-01-fabricated-file | tooled-typed | 1 | 0 | 2 | 0 | y | 5 |
| easy-01-fabricated-file | tooled-typed | 1 | 0 | 2 | 0 | y | 4 |
| easy-01-fabricated-file | tooled-typed | 1 | 0 | 2 | 0 | y | 5 |
| easy-02-wrong-constant | tooled | 1 | 0 | 1 | 0 | y | 3 |
| easy-02-wrong-constant | tooled | 1 | 0 | 1 | 0 | y | 4 |
| easy-02-wrong-constant | tooled | 1 | 0 | 1 | 0 | y | 4 |
| easy-02-wrong-constant | tooled-typed | 1 | 0 | 1 | 0 | y | 4 |
| easy-02-wrong-constant | tooled-typed | 1 | 0 | 1 | 0 | y | 3 |
| easy-02-wrong-constant | tooled-typed | 1 | 0 | 1 | 0 | y | 4 |
| hard-01-mixed | tooled | 1 | 0 | 2 | 0 | y | 5 |
| hard-01-mixed | tooled | 1 | 0 | 3 | 0 | y | 4 |
| hard-01-mixed | tooled | 1 | 0 | 3 | 0 | y | 3 |
| hard-01-mixed | tooled-typed | 1 | 0 | 1 | 0 | y | 4 |
| hard-01-mixed | tooled-typed | 1 | 0 | 0 | 0 | y | 4 |
| hard-01-mixed | tooled-typed | 1 | 0 | 3 | 0 | y | 4 |
| hard-02-line-drift | tooled | 1 | 0 | 1 | 0 | y | 4 |
| hard-02-line-drift | tooled | 1 | 0 | 1 | 0 | y | 3 |
| hard-02-line-drift | tooled | 1 | 0 | 1 | 0 | y | 3 |
| hard-02-line-drift | tooled-typed | 1 | 0 | 1 | 0 | y | 4 |
| hard-02-line-drift | tooled-typed | 1 | 0 | 1 | 0 | y | 4 |
| hard-02-line-drift | tooled-typed | 1 | 0 | 1 | 0 | y | 3 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 0 | y | 4 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 0 | y | 4 |
| medium-01-nonexistent-symbol | tooled | 1 | 0 | 0 | 0 | y | 4 |
| medium-01-nonexistent-symbol | tooled-typed | 1 | 0 | 0 | 0 | y | 5 |
| medium-01-nonexistent-symbol | tooled-typed | 1 | 0 | 0 | 0 | y | 4 |
| medium-01-nonexistent-symbol | tooled-typed | 1 | 0 | 0 | 0 | y | 5 |
| medium-02-all-true | tooled | 0 | 0 | 3 | 0 | - | 4 |
| medium-02-all-true | tooled | 0 | 0 | 3 | 0 | - | 3 |
| medium-02-all-true | tooled | 0 | 0 | 3 | 0 | - | 3 |
| medium-02-all-true | tooled-typed | 0 | 0 | 2 | 0 | - | 3 |
| medium-02-all-true | tooled-typed | 0 | 0 | 3 | 0 | - | 3 |
| medium-02-all-true | tooled-typed | 0 | 0 | 3 | 0 | - | 3 |
