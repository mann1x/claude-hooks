import sys
from collections import Counter
words = sys.stdin.read().split()
c = Counter(words)
best = min(c, key=lambda w: (-c[w], w))
print(best)
