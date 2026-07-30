import sys
text = sys.stdin.read().lower()
print(sum(1 for c in text if c in 'aeiou'))
