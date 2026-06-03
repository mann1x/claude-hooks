import sys
tok = sys.stdin.read().split()[0]
print(sum(int(c) for c in tok))
