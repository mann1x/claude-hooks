import sys
nums = sorted(int(x) for x in sys.stdin.read().split())
print(nums[len(nums) // 2])
