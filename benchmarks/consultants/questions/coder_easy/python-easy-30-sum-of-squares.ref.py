import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sum(x * x for x in nums))
