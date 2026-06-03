import sys
nums = sorted(int(x) for x in sys.stdin.read().split())
print(" ".join(str(x) for x in nums))
