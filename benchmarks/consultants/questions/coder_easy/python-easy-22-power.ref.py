import sys
a, b = (int(x) for x in sys.stdin.read().split()[:2])
print(a ** b)
