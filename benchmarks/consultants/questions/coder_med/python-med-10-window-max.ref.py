import sys
from collections import deque
nums = list(map(int, sys.stdin.read().split()))
k = nums[0]
a = nums[1:]
dq = deque()
out = []
for i, x in enumerate(a):
    while dq and a[dq[-1]] <= x:
        dq.pop()
    dq.append(i)
    if dq[0] <= i - k:
        dq.popleft()
    if i >= k - 1:
        out.append(a[dq[0]])
print(" ".join(map(str, out)))
