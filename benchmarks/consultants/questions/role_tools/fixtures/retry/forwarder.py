"""Upstream forwarder."""

from retry import CircuitBreaker, compute_backoff, should_retry

KEEPALIVE_SECONDS = 15


def forward(request, send):
    breaker = CircuitBreaker()
    elapsed = 0.0
    for attempt in range(1, 16):
        if breaker.is_open():
            return {"status": 503, "reason": "breaker open"}
        resp = send(request)
        if not should_retry(resp["status"], elapsed):
            return resp
        breaker.record_failure()
        elapsed += compute_backoff(attempt)
    return {"status": 502, "reason": "retries exhausted"}
