import sys
best = ""
for w in sys.stdin.read().split():
    if len(w) > len(best):
        best = w
print(best)
