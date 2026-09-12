"""Retry helpers for the upstream forwarder."""

MAX_ATTEMPTS = 15
DEADLINE_SECONDS = 90.0


def compute_backoff(attempt, base=0.5, cap=30.0):
    """Full-jitter exponential backoff."""
    import random
    exp = min(cap, base * (2 ** attempt))
    return random.uniform(0, exp)


def should_retry(status, elapsed):
    """True when another attempt is worth making."""
    if elapsed >= DEADLINE_SECONDS:
        return False
    return status in (429, 500, 502, 503, 529)


class CircuitBreaker:
    """Cross-session breaker; opens after repeated upstream failures."""

    def __init__(self, threshold=5):
        self.threshold = threshold
        self.failures = 0

    def record_failure(self):
        self.failures += 1

    def is_open(self):
        return self.failures >= self.threshold
