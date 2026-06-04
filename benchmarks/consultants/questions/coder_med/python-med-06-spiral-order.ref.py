import sys
nums = list(map(int, sys.stdin.read().split()))
R, C = nums[0], nums[1]
g = [nums[2 + i*C: 2 + (i+1)*C] for i in range(R)]
top, bot, left, right = 0, R-1, 0, C-1
out = []
while top <= bot and left <= right:
    for j in range(left, right+1):
        out.append(g[top][j])
    top += 1
    for i in range(top, bot+1):
        out.append(g[i][right])
    right -= 1
    if top <= bot:
        for j in range(right, left-1, -1):
            out.append(g[bot][j])
        bot -= 1
    if left <= right:
        for i in range(bot, top-1, -1):
            out.append(g[i][left])
        left += 1
print(" ".join(map(str, out)))
