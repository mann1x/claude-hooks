import sys
nums = list(map(int, sys.stdin.read().split()))
n = nums[0]
pts = nums[1:1 + 2 * n]
iv = sorted((pts[2*i], pts[2*i+1]) for i in range(n))
out = []
for s, e in iv:
    if out and s <= out[-1][1]:
        out[-1] = (out[-1][0], max(out[-1][1], e))
    else:
        out.append((s, e))
print(" ".join(str(x) for s, e in out for x in (s, e)))
