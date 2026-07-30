import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sum(1 for x in nums if x % 2 == 0))
