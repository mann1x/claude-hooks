# M-B role-tools Tier 1 — 2026-08-01

Three runs, same model (`gemma4:31b-cloud`), 3 trials × 6 questions ×
2 arms each. Read them in order: the first two are kept because they
are the evidence for *why* the third is the number, not because they
are alternative results.

| dir | suite | recall (un→tooled) | precision (tooled) | tool calls | what changed |
|---|---|---|---|---|---|
| `role-tools-tier1` | v1.0 | 0.0% → 6.7% | 20.0% | **0** | baseline — no prompt addendum |
| `role-tools-tier1-addendum` | v1.0 | 13.3% → 60.0% | 69.2% | 32 | addendum added |
| `role-tools-tier1-v1.1` | v1.1 | 0.0% → 73.3% | 78.6% | 28 | corpus cite errors fixed |
| `role-tools-tier1-v1.2` | v1.2 | 6.7% → **60.0%** | **100.0%** | 32 | unscorable controls dropped |

**`role-tools-tier1-v1.2` is the citable result.**

## Run 1 — the knob did nothing

Zero tool calls across 18 tooled trials. The surface was live (11 specs
in every payload; the registry answers correctly when called directly)
and the model declined it every time.

The cause was in the prompts, not the plumbing. `CRITIC_SYSTEM` frames
the job as a routing decision and says *"Default to ready unless you can
name a concrete missing fact… each extra round costs another full agent
loop"* — a model reading that has been told investigating is expensive
and the safe answer is yes. `ADVERSARY_SYSTEM` was worse: explicitly
asked to catch "fabricated or mis-attributed `path:line` citations"
while having no way to check one.

So this run is a **null result, not a negative one**. It measures a
prompt gap. Anyone citing it as "tools don't help the critic" has the
wrong conclusion.

Fix: `council.build_tool_addendum()` — a per-role directive appended to
the system turn only when the role is actually handed tools, so the
default-config prompt stays byte-identical.

## Runs 2–3 — the corpus was wrong, not the model

Run 2's four "false positives" were the critic being right. v1.0 shipped
with cites pointing at the wrong line — `CircuitBreaker` at `retry.py:20`
(actually 21), `is_open()` at `:29` (actually 31), and, worst,
`forwarder.py:9` in the **control**, where the only correct number of
flags is zero. The tooled critic looked them up, reported the drift, and
the harness scored it against them.

Run 3 fixed the cites (suite v1.1) and precision rose to 78.6%. The
residue was an oracle artifact: the remaining flags were tokens named
*inside* a correct catch — "there is no `reset_breaker`; the class only
implements `record_failure` and `is_open`" doubts one token and confirms
another, and a proximity window cannot tell them apart. A token that
cannot be flagged independently of the falsehood beside it is not a
usable control, so v1.2 drops those two.

Both defects are now enforced by tests
(`test_every_cite_in_the_research_resolves`,
`test_no_true_token_shares_a_sentence_with_a_false_one`) because
hand-auditing is exactly what missed them.

## The v1.2 result

```
                     untooled    tooled
recall                   6.7%     60.0%
precision              100.0%    100.0%
false positives             0         0
tool calls                  0        32
completion tokens        1207      1900   (+57%)
wall (s)                 55.6      71.0   (+28%)
```

Per question (caught / planted, 3 trials):

| question | untooled | tooled |
|---|---|---|
| easy-01-fabricated-file | 0/3 | **3/3** |
| easy-02-wrong-constant | 0/3 | 1/3 |
| medium-01-nonexistent-symbol | 1/3 | **3/3** |
| medium-02-all-true (control) | 0 flags | 0 flags |
| hard-01-mixed | 0/3 | **2/3** |
| hard-02-line-drift | 0/3 | 0/3 |

### Against the rubric

| threshold | value | result |
|---|---|---|
| `min_delta_over_untooled` | 0.30 | **+0.533** ✅ |
| `precision_floor` | 0.80 | **1.00** ✅ |
| `recall_floor` | 0.70 | 0.60 ⚠️ (0.733 in v1.1) |

Recall sits just under its floor and is a documented lower bound — the
oracle scores a catch only when the verdict names the fabricated token
near a doubt word.

### The failure mode is coherent, and it is not randomness

The tooled critic verifies **existence** and not **equality**. It greps
the symbol, finds it, and stops:

- `easy-02` claims `MAX_ATTEMPTS = 5`; the value is 15. The critic reads
  line 3 and usually reports the claim as fine.
- `hard-02` cites `should_retry` at `retry.py:1`; it is at line 14. One
  transcript reads: *"`MAX_ATTEMPTS = 15` is defined at `retry.py:3` and
  `should_retry` … is defined at `retry.py:14`"* — it fetched the right
  line, silently corrected it, and called the claims accurate.

Both misses are one directive away, and both are the citation errors
most likely to survive to a user. Worth a v1.3 directive pass before
reading 60% as this model's ceiling.

### Cost

+57% completion tokens for **one** role at 1 lane. The critic fans out
per lane at the x-tiers, so `all_roles` at `xhigh` multiplies this by
roles × lanes. Tier 2 (the council-level cost A/B) is the arm that
should decide the default; `tool_executor` cleared its absolute floors
too and the live A/B flipped it straight back off.

## Reproduce

```
python -m benchmarks.consultants.role_tools_bench --tier detect --live \
    --model gemma4:31b-cloud --trials 3 --out <dir>
```

Verdict text is stored per trial in `trials.jsonl` from v1.1 onward —
run 1 and 2 predate that and cannot be re-audited, which is why the
field exists.
