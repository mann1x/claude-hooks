import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sorted(set(nums), reverse=True)[1])
