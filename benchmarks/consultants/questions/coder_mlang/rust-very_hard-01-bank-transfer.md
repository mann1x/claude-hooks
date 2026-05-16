---
id: rust-very_hard-01-bank-transfer
tier: very_hard
source: concurrency
task: 'Thread-safe Bank::transfer with deadlock-free Mutex strategy under random concurrent transfers. Stdlib only.'
sandbox_path: solution.rs
oracle: rust-very_hard-01-bank-transfer-oracle.py
---

# rust-very_hard-01-bank-transfer

Implement a thread-safe `Bank` type that supports concurrent
**transfers between accounts** without deadlock.

## Behavior

The Bank holds a fixed set of accounts identified by `u32` ids.
Multiple threads call `bank.transfer(from, to, amount)`
concurrently. Each transfer must:

- Atomically debit `from` and credit `to`.
- Be invisible from other threads until both legs are applied
  (no observer sees `from` debited but `to` not credited).
- Never deadlock — even when one thread does `transfer(a, b, …)`
  while another does `transfer(b, a, …)` simultaneously.

## I/O contract

The program reads a transcript from stdin and replays it across
N worker threads. The transcript format:

```
<n_accounts> <n_threads> <n_txns>
<initial_balance_for_acct_0> <initial_balance_for_acct_1> ...
<from_0> <to_0> <amount_0>
<from_1> <to_1> <amount_1>
...
```

Then the program:
1. Constructs the bank with the given initial balances.
2. Splits the txn list round-robin into N threads.
3. Each thread calls `bank.transfer(...)` for each of its txns
   in order. If a transfer would overdraw an account, it's
   silently skipped (no panic; balance must stay non-negative).
4. After all threads `.join()`, the program prints the final
   balances, one per line, in account order.

## Required guarantee

The Bank's locking strategy must be **deadlock-free** under
arbitrary concurrent `transfer(a, b, ...)` / `transfer(b, a, ...)`
patterns from many threads. The oracle stresses this with random
cross-direction transfers; any deadlock-prone strategy produces
rc=124 (timeout) and fails.

## Constraints

- Single file `solution.rs`. Compiled via
  `rustc -O -o sol solution.rs`.
- Stdlib only — `std::sync::Mutex`, `std::sync::Arc`,
  `std::thread`.
- No external crates (`parking_lot`, `crossbeam`, etc.).
- Conservation: sum of balances after all transfers must equal
  sum of initial balances. No account may go negative.
