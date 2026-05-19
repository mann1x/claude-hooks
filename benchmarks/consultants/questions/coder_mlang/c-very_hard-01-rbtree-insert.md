---
id: c-very_hard-01-rbtree-insert
tier: very_hard
source: cs-classic
task: "Red-black tree insert with rebalancing. After all inserts: in-order traversal + 'ok'/'bad' invariants verifier."
sandbox_path: solution.c
oracle: c-very_hard-01-rbtree-insert-oracle.py
---

# c-very_hard-01-rbtree-insert

## ⚠️ CRITICAL CONSTRAINTS — the oracle greps the source

Your solution **must satisfy these literally**:

1. The color enum **must contain the literal tokens `RED` and
   `BLACK`** in the source. The oracle greps for both. Don't
   use 0/1 ints, don't use `kRed`/`kBlack`, don't `#define`.
2. Define the types **verbatim**: `typedef enum { RED, BLACK }
   Color;` and a `Node` struct with `int val; Color color;
   struct Node *left, *right;`.
3. Implement these function signatures verbatim:
   `Node *insert(Node *root, int val);`,
   `void inorder(Node *root, int *out, int *idx);`,
   `int verify_rb(Node *root);`, `void free_tree(Node *root);`.
4. Output **exactly two lines** per run:
   - line 1: space-separated in-order values, **NO `In-order:`
     prefix, NO trailing space**.
   - line 2: literal `ok` (RB valid) or `bad` (invariants
     broken). Nothing else.

Implement a left-leaning red-black tree's **insert** operation
in C, with full rebalancing.

## I/O contract

Stdin:

```
<n>
<x_1> <x_2> ... <x_n>
```

Insert each value in order. After all inserts, output the
tree's **in-order traversal** on one line, space-separated,
followed by an **invariants line**:

```
<sorted values...>
ok
```

If the tree violates any RB invariant after the last insert,
print `bad` instead of `ok`. (The oracle checks this — a tree
that's structurally OK but doesn't preserve red-black
invariants must catch itself.)

Required invariants the verifier must check:

1. The root is BLACK.
2. No RED node has a RED child (no red-red).
3. Every root-to-NULL path has the same black-height.
4. (Optional: if implementing left-leaning, no right-leaning
   red link — but the oracle doesn't enforce this, so any RB
   flavor passes.)

## API

Internal types + functions:

```c
typedef enum { RED, BLACK } Color;
typedef struct Node {
    int val;
    Color color;
    struct Node *left, *right;
} Node;

Node *insert(Node *root, int val);  // returns new root
void inorder(Node *root, int *out, int *idx);
int verify_rb(Node *root);           // 1 if valid, 0 if bad
void free_tree(Node *root);
```

## Constraints

- Single file `solution.c`. Compiled via
  `gcc -O2 -Wall -o sol solution.c -lm`.
- Use `malloc`/`free` for nodes. No leaks (oracle doesn't check
  with valgrind but expects a `free_tree` call before exit).
- Stdlib only — no third-party tree libraries.

