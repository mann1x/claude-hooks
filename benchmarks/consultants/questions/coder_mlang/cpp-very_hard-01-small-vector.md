---
id: cpp-very_hard-01-small-vector
tier: very_hard
source: cpp-idiom
task: 'SmallVector<T,N> template with stack-then-heap storage. Placement new, move semantics. NO std::vector.'
sandbox_path: solution.cpp
oracle: cpp-very_hard-01-small-vector-oracle.py
---

# cpp-very_hard-01-small-vector

## ⚠️ CRITICAL CONSTRAINTS — the oracle greps the source

Your solution **must satisfy these literally**:

1. Define `template <typename T, std::size_t N> class
   SmallVector` with the **exact methods** in the API section
   below (signatures, names, return types).
2. **No `std::vector`** anywhere in the source — the oracle
   greps for `std::vector` and rejects the file if found.
3. Storage must be **stack-then-heap** with a `bool is_inline()`
   method returning `true` while `size() <= N`.
4. The heap path uses raw allocation
   (`operator new[](capacity_ * sizeof(T))`) + manual
   placement-new + manual destructor calls.
5. **Output format** for the `print` op: `<size> <is_inline>`
   space-separated (where `is_inline` is `1` or `0`), one
   line per `print`, **no headers, no labels**. Example
   output for the README I/O contract:
   `2 1` then newline then `4 1` then newline.
6. The program must **compile cleanly** under
   `g++ -O2 -std=c++17 -Wall -Wextra -lpthread`. Common
   gotchas: alignment for the inline buffer (use
   `alignas(T)` or `std::aligned_storage`), destructor order
   on move, exception-safety on growth.

Implement a `SmallVector<T, N>` template with **stack-then-heap
storage**: the first `N` elements live in an aligned inline
buffer; beyond that the container heaps onto the free store.

## API

```cpp
template <typename T, std::size_t N>
class SmallVector {
public:
    SmallVector();                    // empty
    ~SmallVector();                   // destroy all live elements
    SmallVector(const SmallVector&) = delete;             // simplified
    SmallVector& operator=(const SmallVector&) = delete;  // simplified
    SmallVector(SmallVector&&) noexcept;                  // move ctor
    SmallVector& operator=(SmallVector&&) noexcept;       // move assign

    void push_back(const T& v);
    void push_back(T&& v);
    void pop_back();
    std::size_t size() const noexcept;
    std::size_t capacity() const noexcept;
    bool is_inline() const noexcept;  // true iff using stack buffer
    T& operator[](std::size_t i);
    const T& operator[](std::size_t i) const;
};
```

## Required semantics

- `is_inline()` returns true while `size() <= N` and the
  container is using the stack buffer. As soon as
  `push_back` would make `size() > N`, it migrates everything
  to the heap; `is_inline()` returns false thereafter.
- The heap path uses `operator new[](capacity_ * sizeof(T))` +
  manual placement-new + manual destructor calls — **NOT**
  `std::vector<T>` (the oracle greps for `std::vector`).
- Heap capacity grows geometrically (e.g. doubling).
- Destructor must destroy all live elements (one call to `T`'s
  destructor each) and release the heap buffer if non-inline.
- Move ctor / move assign transfer ownership and leave the
  source empty (`size() == 0`, inline).
- `pop_back` destroys the popped element.

## I/O contract (driver in main)

The program reads from stdin:

```
<n_ops>
<op_0>
<op_1>
...
```

Each op is one of:

- `push <int>` — `push_back(int)`
- `pop` — `pop_back()`
- `print` — print `size()` and `is_inline()` (`1` or `0`),
  space-separated, then a newline

`main` constructs a `SmallVector<int, 4>` (small inline
buffer of 4) and applies the ops in order. Use the int
template parameter so the test doesn't need destructor
gymnastics.

Example:

```
$ printf "6\npush 1\npush 2\nprint\npush 3\npush 4\nprint\n" | ./sol
2 1
4 1
```

(2 elements, inline; then 4 elements, still inline at exactly N.)

```
$ printf "7\npush 1\npush 2\npush 3\npush 4\npush 5\nprint\npop\n" | ./sol
5 0
```

(5 elements, NOT inline anymore.)

## Constraints

- Single file `solution.cpp`. Compiled via
  `g++ -O2 -std=c++17 -Wall -o sol solution.cpp -lpthread`.
- **No `std::vector`** — that's the whole point. Use raw
  `operator new`/`operator delete` for the heap path.
- `<new>`, `<utility>`, `<cstddef>`, `<iostream>` allowed.

