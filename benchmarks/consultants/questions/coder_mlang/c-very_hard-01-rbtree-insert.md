# c-very_hard-01-rbtree-insert

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

