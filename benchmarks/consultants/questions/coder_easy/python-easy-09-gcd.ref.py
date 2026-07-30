import sys
a, b = (int(x) for x in sys.stdin.read().split()[:2])
while b:
    a, b = b, a % b
print(a)
