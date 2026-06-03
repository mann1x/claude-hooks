import sys
line = sys.stdin.readline().rstrip('\n').rstrip('\r')
print("yes" if line == line[::-1] else "no")
