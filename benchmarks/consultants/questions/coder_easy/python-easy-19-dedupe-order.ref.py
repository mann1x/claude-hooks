import sys
seen = set()
out = []
for x in sys.stdin.read().split():
    if x not in seen:
        seen.add(x)
        out.append(x)
print(" ".join(out))
