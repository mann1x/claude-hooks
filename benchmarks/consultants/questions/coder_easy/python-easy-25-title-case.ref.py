import sys
words = sys.stdin.read().split()
print(" ".join(w[0].upper() + w[1:].lower() for w in words))
