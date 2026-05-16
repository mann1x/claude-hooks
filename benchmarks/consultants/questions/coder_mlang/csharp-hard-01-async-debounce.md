---
id: csharp-hard-01-async-debounce
tier: hard
source: csharp-idiom
task: 'Async debouncer<T> with CancellationTokenSource — coalesces rapid pushes; emits latest after 100ms quiet window. FlushAsync on EOF.'
sandbox_path: solution.cs
oracle: csharp-hard-01-async-debounce-oracle.py
---

# csharp-hard-01-async-debounce

Implement an **async debounce wrapper** that consumes a stream
of integers from stdin separated by delays, and emits only the
"settled" value after a quiet window.

## I/O contract

Stdin: lines of the form

```
sleep <ms>
push <int>
```

`sleep` pauses the producer for `<ms>` milliseconds.
`push` enqueues `<int>` into the debouncer. EOF flushes pending
events and the program exits.

The debouncer has a fixed window of **100 ms**: it emits the
**latest** value that arrived if no new value has arrived in the
last 100 ms. Values that arrive within 100 ms of the previous
arrival are coalesced into one emission with the LATEST value.

Stdout: one emitted value per line, in emission order.

Example:

```
$ printf "push 1\nsleep 50\npush 2\nsleep 50\npush 3\nsleep 200\npush 4\n" | ./sol
3
4
```

(Values 1, 2, 3 are coalesced because each pair is < 100 ms
apart; 3 emits after the 200 ms quiet window; 4 arrives but
the program exits before the next quiet window, so the final
flush emits 4.)

## Required idiom

Implement as a class:

```csharp
public class Debouncer<T> {
    private readonly TimeSpan _window;
    private CancellationTokenSource _cts;
    private T _pending;
    private bool _hasPending;
    private readonly Func<T, Task> _onEmit;
    // ...
    public Debouncer(TimeSpan window, Func<T, Task> onEmit);
    public void Push(T value);          // resets the timer
    public async Task FlushAsync();     // emit pending immediately
}
```

- `Push` resets a per-Debouncer timer; on each push, cancel the
  pending timer and start a new one for `_window`.
- The timer's callback emits the latest `_pending` via `_onEmit`
  and clears the flag.
- `FlushAsync` cancels the timer and emits any pending value
  synchronously (well, `await`-ably).

`Program.Main` constructs the Debouncer, parses stdin, and
calls `Push` / sleep accordingly. After EOF, calls `FlushAsync`.

## Constraints

- Single file `solution.cs`. Built via `dotnet publish -c Release`.
- Stdlib only — `System`, `System.Threading.Tasks`,
  `System.Collections.Generic`.
- Must use `async`/`await` and `CancellationToken` —
  `Thread.Sleep` on the main path is a fail.
- The oracle greps for `CancellationTokenSource` to confirm
  the idiom.

