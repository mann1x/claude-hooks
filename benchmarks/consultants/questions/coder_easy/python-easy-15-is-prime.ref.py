import sys
n = int(sys.stdin.read().split()[0])
if n < 2:
    print("no")
else:
    p = True
    i = 2
    while i * i <= n:
        if n % i == 0:
            p = False
            break
        i += 1
    print("yes" if p else "no")
