import sys
s = sys.stdin.readline().rstrip("\n").rstrip("\r")
out = []
i, n = 0, len(s)
while i < n:
    j = i
    while j < n and s[j] == s[i]:
        j += 1
    out.append(s[i] + str(j - i))
    i = j
print("".join(out))
