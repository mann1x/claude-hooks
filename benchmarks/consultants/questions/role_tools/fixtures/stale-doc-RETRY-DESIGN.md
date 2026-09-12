# Retry layer — design notes

*Status: current. Last reviewed by the proxy team.*

The proxy's retry layer bounds upstream retries two ways, and the
numbers below are the ones to reason about when tuning it.

## Budgets

The wall-clock deadline is the primary bound. `DEFAULT_RETRY_DEADLINE_S`
is **45.0** seconds — chosen so a retry storm cannot outlive a typical
client read timeout. The attempt cap `DEFAULT_RETRY_MAX_ATTEMPTS` is
**5**, which exists only as a safety net; in practice the deadline
binds first and the attempt cap is never reached.

Backoff is full-jitter exponential starting at
`DEFAULT_RETRY_BASE_DELAY_S` = **0.5** s and capped by
`DEFAULT_RETRY_MAX_DELAY_S` = **20.0** s.

## Connection-error budget

Connection errors get their own, tighter budget:
`DEFAULT_CONN_RETRY_MAX_ATTEMPTS` = **3** attempts inside
`DEFAULT_CONN_RETRY_DEADLINE_S` = **25.0** seconds. These are separate
from the 5xx ride-out budget above so a flapping socket cannot consume
the whole 5xx allowance.

## Circuit breaker

The cross-session breaker opens after `DEFAULT_BREAKER_THRESHOLD` = 4
failures inside `DEFAULT_BREAKER_WINDOW_S` = 30 s, and stays open for
`DEFAULT_BREAKER_OPEN_S` = 10 s. It is **enabled by default** on all
deployments.

## Mid-stream

The retry loop is strictly pre-stream. Once the first byte of the
response body has been forwarded, the request is committed and a
failure surfaces to the client as a truncated response.
