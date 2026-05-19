# Consultancy Skill-Eval — Multi-Language Coder Suite (`coder_mlang`)

A sibling of the `coder` suite (v1.0, Python-only HumanEval-style)
that stresses code generation across the languages the user
actually writes: **Python, Rust, Go, C, C++, C#**. Questions are
sourced from LCB / MultiPL-E patterns rather than HumanEval, and
weighted toward problems that require reasoning rather than
template-matching — the 2026-05-16 `coder@1.0` run had three of
four models tied at avg_quality ≥ 4.5 and pass_rate 100%, so the
suite stopped discriminating at the easy/medium tiers. This suite
is designed to **break that ceiling**.

This document is the canonical design. The companion ledger
(`docs/consultants-skill-eval-baselines.md`) gets a new
`Coder (multi-language)` section once the first live run lands.

---

## Why a second suite, not a v2 of `coder`

The protocol's versioning rules say:

- **PATCH** = oracle tightened
- **MINOR** = question added, every prior model re-baselined
- **MAJOR** = rubric changed

A move from Python-only to multi-language is none of those.
Adding Rust questions doesn't change `coder@1.0`'s validity for
the "pick a Python coder" question — `glm-5.1:cloud`'s 4.88 stays
defensible because nobody asked it to write Rust. A **separate
suite** keeps the v1 baseline durable and lets the multi-language
question be answered independently. The two coexist; nothing
shipping in this suite invalidates the prior commit.

Naming follows the existing layout:

```
benchmarks/consultants/questions/
├── coder/                    # v1.0 — Python HumanEval-style
│   ├── SUITE.md
│   └── … question files
└── coder_mlang/              # v1.0 — multi-language stress
    ├── SUITE.md
    └── … question files
```

The same `claude-consultants skill-eval coder` CLI runs both:
`--questions-dir benchmarks/consultants/questions/coder_mlang/`
swaps the input. (A future PATCH may introduce a `--suite mlang`
shorthand — out of scope for v1 of mlang.)

---

## Model cohort

The 2026-05-16 `coder@1.0` run leaves `glm-5.1:cloud` and
`kimi-k2.6:cloud` essentially tied for the top spot (within 0.02
avg_quality). The cohort for `coder_mlang@1.0` is **the four
the user named plus the two top v1 finishers**:

| Model                          | Why included                                          |
|--------------------------------|--------------------------------------------------------|
| `glm-5.1:cloud`                | v1 rubric winner — defending champion                  |
| `kimi-k2.6:cloud`              | v1 runner-up — defending champion                      |
| `deepseek-v4-flash:cloud`      | new candidate; 2026-05-09 PROD-screen validated        |
| `deepseek-v4-pro:cloud`        | new candidate; coder-oriented family                   |
| `minimax-m2.7:cloud`           | new candidate; coder/general hybrid                    |

The two v1 leaders need to be re-scored on the harder questions
to find out whether they actually understand each language or
just glided through Python-trivial tasks. Without them in the
cohort, a hypothetical "deepseek-v4-pro wins mlang" verdict
couldn't be compared cleanly against v1 — same models, two
suites, the relative ordering is the real signal.

A pre-run smoke fires the cohort against `coder@1.0` trivial
tier to validate the 3 new identifiers (`deepseek-v4-flash`,
`deepseek-v4-pro`, `minimax-m2.7`) resolve on the
`192.168.178.2:11433` proxy. The smoke output is _not_ scored
into either suite's ledger — its job is solely to fail fast on a
name typo (the 2026-05-16 `qwen3-next:cloud` → `qwen3-coder-next:cloud`
discovery is the cautionary tale; see
[`reference_ollama_pro_cloud_names`](../../../root/.claude/projects/-srv-dev-disk-by-label-opt-dev-claude-hooks/memory/reference_ollama_pro_cloud_names.md)).

---

## Languages + toolchain pinning

Verified on solidpc 2026-05-16:

| Language | Compiler / runtime           | Version             | Source ext  | Sandbox file        |
|----------|------------------------------|---------------------|-------------|---------------------|
| Python   | `cpython` (conda env)        | 3.11.15             | `.py`       | `solution.py`       |
| Rust     | `rustc`                      | 1.89                | `.rs`       | `solution.rs`       |
| Go       | `go build` / `go run`        | 1.22.3              | `.go`       | `solution.go`       |
| C        | `gcc`                        | 10.2                | `.c`        | `solution.c`        |
| C++      | `g++ -std=c++17`             | 10.2                | `.cpp`      | `solution.cpp`      |
| C#       | `dotnet run` (top-level prog) | 8.0.417             | `.cs`       | `solution.cs`       |

Toolchain versions are captured in the run metadata so a future
re-baseline against a newer Rust / Go / dotnet can be detected.
Toolchain mismatch between hosts triggers a warning in the
report.

C# is the most ceremonious: dotnet wants a project file. The
oracle's compile path generates a minimal `solution.csproj`
on-the-fly and invokes `dotnet run` against the trial sandbox
— the model only has to produce `solution.cs` containing
top-level statements (C# 9+) or a `Program` class with
`Main`. This is documented in each C# question's task.md.

---

## Question count + tier shape

`coder@1.0` had 8 questions × 4 tiers = 32 trials/model. That's
too coarse for stress — half of those tiers stopped
discriminating. For `coder_mlang@1.0`:

| Tier        | Per-language | Total questions | Total trials × 5 models |
|-------------|-------------:|----------------:|-------------------------:|
| `medium`    | 1            | 6 (1/lang)      | 30                       |
| `hard`      | 1            | 6 (1/lang)      | 30                       |
| `very_hard` | 1            | 6 (1/lang)      | 30                       |

**18 questions, 90 trials full run.** Smoke = `medium` tier only
× 5 models = 30 trials. Token cost projection: ~3K tokens/trial
× 90 = ~270K full run, ~90K smoke. Well within Ollama Pro
weekly quota at present usage.

Tier semantics for the mlang suite (different from `coder@1.0` —
no trivial/easy at all):

- **medium**: idiomatic single-function problem with 1-2 edge
  cases (e.g. flatten a nested structure, parse a date). Tests
  whether the model knows the language's idioms — does it use
  iterators or write a manual loop in Rust? Does it use slices
  in Go? Does it use `vector<>` and `unique_ptr<>` in C++?
- **hard**: multi-step algorithmic problem requiring reasoning
  about edge cases (e.g. LRU cache, recursive descent parser,
  3-way quicksort). Solutions span ~20-50 lines.
- **very_hard**: design + concurrency + correctness across
  threads (e.g. thread-safe bank transfer with deadlock
  prevention, async debounce with cancellation, rate limiter).
  Solutions span ~50-100 lines. Forces the model to reason about
  ordering, atomicity, and ownership.

---

## Question manifest (v1.0 draft)

Question IDs follow `<lang>-<tier>-<N>-<slug>`:

### Medium tier (1 per language)

| ID                                  | Task summary                                                    |
|-------------------------------------|-----------------------------------------------------------------|
| `python-medium-01-flatten-nested`   | Flatten arbitrarily-nested list of ints; preserve order         |
| `rust-medium-01-rotate-vec`         | In-place `Vec<i32>` rotation by k, O(1) extra space             |
| `go-medium-01-sum-channels`         | Fan-in sum across 3 chans; respect `ctx.Done()` cancellation    |
| `c-medium-01-strrev-inplace`        | In-place C-string reverse (`char *`, null-terminated)           |
| `cpp-medium-01-string-trim`         | Trim leading/trailing whitespace from `std::string`             |
| `csharp-medium-01-distinct-by`      | `IEnumerable<T>.DistinctBy<TKey>` extension method              |

### Hard tier

| ID                                  | Task summary                                                    |
|-------------------------------------|-----------------------------------------------------------------|
| `python-hard-01-lru-cache`          | `class LRUCache(capacity)` with O(1) `get` + `put`              |
| `rust-hard-01-iter-window-pairs`    | `WindowPairs<I>: Iterator<Item = (I::Item, I::Item)>`           |
| `go-hard-01-worker-pool`            | Bounded-concurrency worker pool with graceful shutdown          |
| `c-hard-01-quicksort-3way`          | Dutch-flag in-place quicksort with median-of-three pivot        |
| `cpp-hard-01-expr-eval`             | Recursive-descent parser for `1+2*3-(4-1)`; returns int         |
| `csharp-hard-01-async-debounce`     | `Debounce<T>` async wrapper batching calls within W ms          |

### Very-hard tier

| ID                                       | Task summary                                                                  |
|------------------------------------------|-------------------------------------------------------------------------------|
| `python-very_hard-01-parser-combinator`  | Minimal parser-combinator library (`Seq`, `Or`, `Many`); parse `a(b|c)*`      |
| `rust-very_hard-01-bank-transfer`        | Thread-safe `Bank::transfer` with deadlock-free `Mutex<HashMap>`              |
| `go-very_hard-01-rate-limiter`           | Token-bucket rate limiter; thread-safe; no goroutine leaks                    |
| `c-very_hard-01-rbtree-insert`           | Red-black tree insert + balance; preserve RB invariants                       |
| `cpp-very_hard-01-small-vector`          | `SmallVector<T, N>` with stack-then-heap storage + move semantics             |
| `csharp-very_hard-01-di-container`       | Minimal constructor-resolution DI container with cycle detection              |

---

## Oracle pattern

Same shape as `coder@1.0`: each question ships a `task.md` and an
`oracle_*.py` that the harness runs via pytest. The Python oracle
is what changes — for non-Python languages it shells out to the
language's compiler/runtime instead of importing the produced
file.

### Python oracle (unchanged from v1.0)

```python
import os, sys
from pathlib import Path
SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))
import solution  # the model is expected to write solution.py

def test_basic_case():
    assert solution.lru_cache_get(c, 1) == 1
    # … etc
```

### Non-Python oracle (NEW for v1.0 mlang)

```python
import os, subprocess, tempfile
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "solution.rs"     # or .go / .c / .cpp / .cs

def _compile_and_run(stdin_input: str = "",
                     argv: list[str] | None = None,
                     timeout_s: int = 15
                     ) -> tuple[int, str, str]:
    """Compile + run the source. Returns (returncode, stdout, stderr).
    Asserts ``SOURCE`` exists with a diagnostic if not."""
    assert SOURCE.is_file(), (
        f"missing {SOURCE.name}; model produced: "
        f"{[p.name for p in SANDBOX.iterdir()]}"
    )
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "sol"
        # Compile step (per-language; example: Rust)
        compile_r = subprocess.run(
            ["rustc", "-O", "-o", str(binary), str(SOURCE)],
            capture_output=True, text=True, timeout=60,
        )
        assert compile_r.returncode == 0, (
            f"rustc failed:\nstderr:\n{compile_r.stderr}"
        )
        # Run step (uniform across languages)
        run_r = subprocess.run(
            [str(binary), *(argv or [])],
            input=stdin_input,
            capture_output=True, text=True, timeout=timeout_s,
        )
        return run_r.returncode, run_r.stdout, run_r.stderr

def test_basic_case():
    rc, out, err = _compile_and_run("1 2 3 4 5\n")
    assert rc == 0, f"runtime error: {err}"
    assert out.strip() == "1 2\n2 3\n3 4\n4 5", f"got: {out!r}"
```

Per-language compile commands (oracles inline these):

| Language | Compile command                                              | Run command                |
|----------|---------------------------------------------------------------|----------------------------|
| Rust     | `rustc -O -o sol solution.rs`                                | `./sol`                    |
| Go       | `go build -o sol solution.go`                                | `./sol`                    |
| C        | `gcc -O2 -Wall -o sol solution.c -lm`                        | `./sol`                    |
| C++      | `g++ -O2 -std=c++17 -Wall -o sol solution.cpp -lpthread`     | `./sol`                    |
| C#       | `dotnet run --project <tmpdir>/solution.csproj` (auto-generated) | (`dotnet run` runs it)  |

The harness exposes one new public helper —
`benchmarks/consultants/oracles_mlang.py:compile_and_run(lang, ...)`
— so oracle files don't have to duplicate the subprocess
ceremony. The helper takes a `lang` arg and dispatches to the
right compile command. Each oracle calls it; the per-question
work is just the test cases.

---

## Sandboxed write expectations

`coder@1.0` allowed the model to write under `<sandbox>/<path>`
with caps:

- `max_file_bytes = 50 KB`
- `max_total_bytes = 1 MB`
- `max_files = 16`

These remain unchanged for `coder_mlang@1.0`. Non-Python
languages tend to be a few KB more verbose than equivalent
Python (Rust + lifetimes, C++ + headers, Go + ceremony), but
50 KB per file is far above any of the 18 questions' expected
solution sizes (~50-100 lines × 80 chars = 4-8 KB).

C# is the exception — `dotnet run` requires a project file. The
oracle creates `solution.csproj` from a template under the
temp build dir (not the sandbox), so the model only writes
`solution.cs`. The csproj contents are:

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>
```

---

## Rubric

Same shape as `coder@1.0`:

> `pass_rate ≥ 70%` AND `avg_quality ≥ 3.5`.
> Tie-broken by `median_tokens` (lower wins).

But the **decision rule** is per-language: the suite recommends
one default model **per language** if a clear winner emerges,
plus a global default for the case where the consultants coder
role doesn't know which language is being asked. The global
default falls out of cross-language averages.

If multiple languages produce different winners, the global
default is the model that wins **most** languages with a
tie-break on Python (the most-used language in the consultants
council and the user's primary language).

The decision lands in
`consultants/engine/coder_defaults.py` as a structured object:

```python
RECOMMENDED_CODER_MODEL_BY_LANGUAGE: dict[str, str] = {
    "python": "...",
    "rust":   "...",
    "go":     "...",
    "c":      "...",
    "cpp":    "...",
    "csharp": "...",
}
RECOMMENDED_CODER_MODEL_GLOBAL: str = "..."
```

`config.py` consults the language map when the planner emits a
language hint (`requires_language: rust` in the coder preamble);
otherwise falls back to `RECOMMENDED_CODER_MODEL_GLOBAL`.

---

## Provenance + reproducibility

Every run produces the same artifact set as `coder@1.0`:

- `trials.jsonl` (ignored by git)
- `report.md` (eligible for `--commit-report`)
- `metadata.json` (eligible)
- `quota.md` (user-authored, eligible)

The metadata adds three fields specific to mlang:

```json
{
  "suite": "coder_mlang",
  "suite_version": "1.0",
  "suite_hash": "<sha256 prefix>",
  ...
  "toolchain_versions": {
    "python": "3.11.15",
    "rustc": "1.89.0",
    "go":    "go1.22.3",
    "gcc":   "10.2.1",
    "g++":   "10.2.1",
    "dotnet": "8.0.417"
  },
  "host_arch": "x86_64-linux-gnu",
  "languages_tested": ["python","rust","go","c","cpp","csharp"]
}
```

A future re-run on a host with a different `rustc` (say, 1.95)
records that fact, so a regression in `rust-very_hard-01-bank-transfer`
between runs is visible against the toolchain change.

---

## Build-out plan

The suite ships in **two commits**:

### Commit 1 (this commit): design + scaffold + smoke prep

- This design doc (`docs/consultants-skill-eval-mlang-suite.md`).
- `benchmarks/consultants/questions/coder_mlang/SUITE.md`
  v1.0 manifest.
- `benchmarks/consultants/oracles_mlang.py` — the shared
  `compile_and_run(lang, ...)` helper + toolchain-version probe.
- 2 sample questions to validate the oracle pattern end-to-end:
  - `python-medium-01-flatten-nested` (Python — sanity check
    that the `coder@1.0` import-the-module path still works).
  - `rust-medium-01-rotate-vec` (Rust — first non-Python oracle
    proving the subprocess pattern works).
- Both with full task.md + oracle_*.py + dry-run reference
  submissions baked into `coder_bench._DRY_RUN_SUBMISSIONS`.
- Tests for the new helper.

### Commit 2: the remaining 16 questions + full live run

- Fill in the rest of the manifest's questions (Go, C, C++,
  C# + the hard / very_hard tiers).
- Pre-run smoke against the 5-model cohort on `coder_mlang@1.0`
  medium tier (30 trials, ~90K tokens).
- Full live run (90 trials, ~270K tokens).
- Append baselines row(s) — one per language + the global.
- Update `consultants/engine/coder_defaults.py` with
  `RECOMMENDED_CODER_MODEL_BY_LANGUAGE` + the global pick.
- Document the language-hint protocol the planner uses.

---

## Cost budget

| Phase                                  | Trials | Tokens est. | Quota impact (Ollama Pro weekly) |
|----------------------------------------|-------:|------------:|------------------------------------|
| 5-model smoke vs `coder@1.0` trivial   |     10 |       ~17K  | < 0.1 pp                           |
| `coder_mlang@1.0` smoke (medium tier)  |     30 |       ~90K  | ~0.2 pp                            |
| `coder_mlang@1.0` full run             |     90 |      ~270K  | ~0.5-0.7 pp                        |

The full mlang run is roughly 3× the 2026-05-16 `coder@1.0`
full run (32 trials × ~3K = ~96K). At the user's current
4.3% weekly usage, the full run still leaves comfortable
headroom; the smoke is essentially free at this scale.

---

## Open questions (resolved at design time)

These were considered and resolved during design (2026-05-16);
captured here for traceability:

- **One suite per language, vs one multi-language suite?**
  One multi-language suite, with the *per-language* rubric
  decision producing 6 recommendations + 1 global. Splitting
  into 6 suites would multiply the protocol overhead (6
  SUITE.md, 6 baselines tables) for no gain — the model cohort
  is the same across languages, and cross-language comparisons
  are part of the value (does `kimi-k2.6:cloud` write better Go
  than C++? — only the unified suite answers that).
- **Why drop `trivial` / `easy` tiers from mlang?**
  v1.0 of `coder` saturated at trivial/easy/medium. The mlang
  cohort is fewer-but-harder-on-purpose: every question must
  discriminate models. Easy questions don't pull weight.
- **Why include C# despite the dotnet ceremony?**
  User-named language. The csproj scaffolding is one-shot in
  the oracle helper, not visible to the model. The cost is one
  template string in `oracles_mlang.py`.
- **What about `qwen3-coder-next:cloud` in the cohort?**
  Disqualified for `coder_mlang@1.0`: v1.0 ranked it last on
  quality (4.50) despite the fastest wall, and the user
  identified the stronger candidates above. If
  `deepseek-v4-pro:cloud` fails mlang, qwen3-coder-next gets
  reconsidered in `coder_mlang@1.1`.
- **What about `gemma4:31b-cloud`?**
  Mid-pack on v1.0 (q=4.62) and the slowest tail. Excluded
  from the mlang cohort to keep the cost down — focus on the
  models with a realistic shot at the global default.
