"""A small, thread-safe rate limiter enforcing a maximum call rate.

Extracted from the inline `time.sleep(REQUEST_DELAY_SECONDS)` pattern
scripts/162_download_xbrl_only_filing.py already used (SEC's own
documented fair-access limit is 10 requests/second) so the throttle is
a single, independently-testable module instead of a duplicated
constant in every network-calling script. Behavior is unchanged: one
`.acquire()` call per outgoing request, called from a single process.

This limiter is intentionally process-local (a `threading.Lock`, not a
cross-process one). SEC downloads in this project run from exactly one
process at a time (see docs/DECISIONS_LOG.md and CURRENT_STATE.md — the
parallel-ingestion work parallelizes Arelle *parsing* of already-locked
filings, never concurrent SEC downloads), so a process-local guarantee
is sufficient to keep the aggregate request rate under the limit; it
also correctly bounds multiple threads within one process, which a bare
`time.sleep` in a shared function does not.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Blocks each `.acquire()` call so calls are spaced at least
    `1 / max_calls_per_second` apart, measured from the START of the
    previous call (matches the pre-existing sleep-after-request
    behavior: a slow request that already took longer than the minimum
    interval does not cause the next call to wait further)."""

    def __init__(self, max_calls_per_second: float) -> None:
        if max_calls_per_second <= 0:
            raise ValueError("max_calls_per_second must be > 0")
        self._min_interval = 1.0 / max_calls_per_second
        self._lock = threading.Lock()
        self._last_call_monotonic: float | None = None

    def acquire(self) -> float:
        """Blocks until it is safe to make the next call. Returns the
        number of seconds actually slept (0.0 if no wait was needed) —
        useful for tests that assert the limiter is doing real work."""
        with self._lock:
            now = time.monotonic()
            if self._last_call_monotonic is None:
                wait_seconds = 0.0
            else:
                elapsed = now - self._last_call_monotonic
                wait_seconds = max(0.0, self._min_interval - elapsed)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_call_monotonic = time.monotonic()
            return wait_seconds


# Shared singleton for every SEC-facing request in this project (SEC's
# documented fair-access limit is 10 requests/second). A single shared
# instance -- not one per module -- is what makes the 10/s bound hold on
# the AGGREGATE request rate when more than one component makes SEC
# requests within the same process (e.g. scripts/162's filing downloader
# and ingestion/cik_resolver.py's company-name search both run during
# point-in-time universe expansion); two independent RateLimiter objects
# would each individually respect 10/s while together doubling it.
SEC_RATE_LIMITER = RateLimiter(max_calls_per_second=10.0)
