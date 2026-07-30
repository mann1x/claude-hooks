import sys
parts = sys.stdin.read().split('\n')
a = parts[0].rstrip('\r')
b = parts[1].rstrip('\r') if len(parts) > 1 else ''
print("yes" if sorted(a) == sorted(b) else "no")
