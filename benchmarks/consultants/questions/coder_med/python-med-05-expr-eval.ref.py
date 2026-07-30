import sys
s = sys.stdin.readline().strip()
# tokenize
toks = []
i = 0
while i < len(s):
    if s[i].isdigit():
        j = i
        while j < len(s) and s[j].isdigit():
            j += 1
        toks.append(int(s[i:j]))
        i = j
    else:
        toks.append(s[i])
        i += 1
# pass 1: collapse '*'
red = [toks[0]]
k = 1
while k < len(toks):
    op = toks[k]
    val = toks[k+1]
    if op == '*':
        red[-1] = red[-1] * val
    else:
        red.append(op)
        red.append(val)
    k += 2
# pass 2: left-to-right + and -
total = red[0]
k = 1
while k < len(red):
    if red[k] == '+':
        total += red[k+1]
    else:
        total -= red[k+1]
    k += 2
print(total)
