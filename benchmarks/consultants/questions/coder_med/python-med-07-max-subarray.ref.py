import sys
a = list(map(int, sys.stdin.read().split()))
best = cur = a[0]
for x in a[1:]:
    cur = max(x, cur + x)
    best = max(best, cur)
print(best)
