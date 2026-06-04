import sys
parts = sys.stdin.read().split()
fb, tb, val = int(parts[0]), int(parts[1]), parts[2]
n = int(val, fb)
if n == 0:
    print("0")
else:
    digs = "0123456789abcdef"
    out = []
    while n > 0:
        out.append(digs[n % tb])
        n //= tb
    print("".join(reversed(out)))
