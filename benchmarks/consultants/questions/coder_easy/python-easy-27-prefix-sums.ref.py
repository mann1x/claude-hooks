import sys
nums = [int(x) for x in sys.stdin.read().split()]
acc = 0
out = []
for x in nums:
    acc += x
    out.append(str(acc))
print(" ".join(out))
