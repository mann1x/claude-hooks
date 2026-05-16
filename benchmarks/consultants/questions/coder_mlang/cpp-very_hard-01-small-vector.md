# cpp-very_hard-01-small-vector

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

