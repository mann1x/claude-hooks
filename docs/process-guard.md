# Process guard

A PreToolUse check, on by default, that denies two kinds of command
before they run:

1. **Commands that kill or match themselves** — `pkill -f PAT`,
   `pgrep -f PAT` (as a condition, a captured PID list, or piped to
   `kill`), `killall`, `ps … | grep PAT | … kill`, when PAT matches the
   command line of a process the command is running inside.
2. **Blind waiters** — a background `until`/`while` poll, or a Monitor
   filter, that can only notice success: if the job dies it waits forever
   and nothing tells the session.

A denial carries what is wrong and how to fix it, and ends with
*"Rewrite the command with the fix and run it again — this does not need
the user."* The session carries on; nothing waits for you, so long runs
and overnight work are not interrupted.

## Why a command matches itself

Every Bash tool call runs as

```
/bin/bash -c "source <snapshot> && … && eval '<your command>' && pwd -P >| …"
```

so for as long as the command runs, a process exists whose command line
**is** the command. `pkill -f run-baseline.sh; nohup ./run-baseline.sh &`
kills that process — the tool reports exit 144 and nothing after the kill
runs. Over ssh, `ssh host 'CMD'` runs `bash -c 'CMD'` on the remote side,
which dies the same way (exit 255). A `while pgrep -f PAT` waiter always
sees itself, so it never ends, and its command line also satisfies every
*other* session's `pgrep -f PAT` on that host.

What was measured (bash 5.1 locally, 5.2 on bs2, 2026-09-29):

| command | outcome |
|---|---|
| `ssh bs2 'pkill -f X'` | safe — bash execs a single command in place |
| `ssh bs2 'cd /tmp; pkill -f X'` | safe — the last command of `a; b` is exec'd too |
| `ssh bs2 'pkill -f X; true'` | **exit 255** — the shell is alive during the kill |
| `ssh bs2 'pkill -f "[x]yz"; true'` | safe |
| `ssh bs2 'pkill -f "[x]yz"; echo xyz'` | **exit 255** — the bare name is in the argv |
| `ssh bs2 'bash -s' <<'EOF' … EOF` | safe — a script on stdin is in no argv |

Locally the wrapper always runs `&& pwd -P` after the command, so the
carrier is always alive. `$(…)` forks keep the shell's argv.

## How it decides

`claude_hooks/shell_ast.py` parses the command the way bash does
(quotes, escapes, `$(…)`, backticks, heredocs, pipelines, loops, `if`,
`case`). `claude_hooks/process_guard.py` walks it, entering every context
the command creates — the remote `bash -c` of each `ssh`, nested
`bash -c`, a heredoc or here-string fed to a shell — and tracks the
processes alive in each: the Bash tool's wrapper, this Claude Code
session and its ancestors (`run.py` reads them from `/proc`), the remote
sshd session and shell. A matcher is judged against those, with the
pattern compiled the way the tool compiles it (procps/`grep -E` ERE,
plain `grep` BRE, awk `/re/ && !/re/`), and later filters in the same
pipeline applied.

It leaves alone, because they are not faults:

- a plain listing (`pgrep -af X`, `ps aux | grep X`, a `for p in $(pgrep …)`
  loop that kills nothing);
- a kill that excludes the shell (`[ "$p" != "$$" ]`, `grep -v $$`, a
  filter such as `grep -v grep` that drops the wrapper's line);
- a loop that inspects each PID before killing it (`/proc/$p/cmdline`);
- `| head -1` (the oldest match is the target, not this shell);
- a backgrounded simple command (`nohup bash x.sh &`), which does not
  carry the wrapper's argv;
- anything it cannot parse, or a pattern it cannot resolve (`"$P"` set
  elsewhere) — no opinion.

A background waiter is covered — not denied — when any exit path checks
liveness (`kill -0`, `pgrep`, `ps -p`, `/proc/$pid`, `systemctl`,
`docker`, `wait`, `tail --pid`), matches failure or an outcome marker
(`error|fail|traceback|killed|exit|status…`, or a `grep -E 'A|B'` that
names several outcomes), or is bounded (`timeout`, a counter, `$SECONDS`,
a `for` loop). A Monitor is covered when anything it prints or checks
does. Foreground loops are bounded by the tool's own timeout and are not
checked.

CronCreate and ScheduleWakeup prompts that check on a job and never
mention failure get one sentence appended (via `updatedInput`, no
permission decision — normal permissions apply): report a failed,
crashed, stalled or vanished job as an outcome. Slash-command prompts and
the `/loop` sentinels are left alone.

## Calibration

Run over 22 312 Bash and Monitor calls from every transcript on solidpc
(April–September 2026), where a real local self-kill is recognisable by
exit 144 (137/1 for `-9`) and output that stops at the kill line:

- self-kill findings cover 385 of the 398 exit-144 and 335 of the 374
  exit-255 kill commands; the remote findings reviewed on commands that
  reported success were real kills whose status `| tail` (or `|| true`)
  masked — their output stops at the kill;
- every false positive found in review is a regression test in
  `tests/test_process_guard.py`;
- 1.5 ms per command on average over that (kill- and loop-heavy) corpus,
  17 ms at worst; 0.1 % of commands do not parse (those inspected were
  bash syntax errors too) and get no opinion.

## Configuration

```json
"hooks": {
  "pre_tool_use": {
    "process_guard": { "enabled": true, "waiters": true, "prompts": true }
  }
}
```

`waiters: false` keeps only the self-kill checks; `prompts: false` stops
the cron/wakeup note. In a project with a `.claude-hooks-disable`
marker, add `guards` to its `keep:` line. A single command can opt out
with a `# process-guard: allow` comment.
