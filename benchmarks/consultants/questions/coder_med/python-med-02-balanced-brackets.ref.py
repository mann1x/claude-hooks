import sys
s = sys.stdin.readline().rstrip("\n").rstrip("\r")
pairs = {")": "(", "]": "[", "}": "{"}
st = []
ok = True
for c in s:
    if c in "([{":
        st.append(c)
    elif c in pairs:
        if not st or st.pop() != pairs[c]:
            ok = False
            break
print("YES" if ok and not st else "NO")
