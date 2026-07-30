import sys
n = int(sys.stdin.read().split()[0])
r = 1
for i in range(2, n + 1):
    r *= i
print(r)
