"""
tests/test_rate_limiter.py -- the "rate limiter never exceeds 10
requests/second" requirement for the parallel-ingestion work. No Arelle,
no network, no filesystem beyond stdlib timing -- always fast.
"""

from __future__ import annotations

import threading
import time

from stock_agent.ingestion.rate_limiter import RateLimiter


def test_first_call_never_waits() -> None:
    limiter = RateLimiter(max_calls_per_second=10)
    waited = limiter.acquire()
    assert waited == 0.0


def test_single_thread_never_exceeds_rate() -> None:
    max_rate = 20.0  # kept high (short intervals) so the test stays fast
    limiter = RateLimiter(max_calls_per_second=max_rate)
    min_interval = 1.0 / max_rate

    timestamps: list[float] = []
    for _ in range(10):
        limiter.acquire()
        timestamps.append(time.monotonic())

    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    epsilon = 0.005  # scheduler jitter tolerance
    for gap in gaps:
        assert gap >= min_interval - epsilon, f"consecutive calls only {gap:.4f}s apart, need >= {min_interval:.4f}s"

    total_elapsed = timestamps[-1] - timestamps[0]
    assert total_elapsed >= (len(timestamps) - 1) * (min_interval - epsilon)


def test_concurrent_threads_never_exceed_aggregate_rate() -> None:
    """The limiter must bound the AGGREGATE call rate across threads,
    not just per-thread -- otherwise N threads each respecting the limit
    independently would together exceed it by a factor of N."""
    max_rate = 20.0
    limiter = RateLimiter(max_calls_per_second=max_rate)
    min_interval = 1.0 / max_rate

    timestamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(5):
            limiter.acquire()
            with lock:
                timestamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(timestamps) == 20
    timestamps.sort()
    epsilon = 0.005
    for a, b in zip(timestamps, timestamps[1:]):
        assert b - a >= min_interval - epsilon, (
            f"two acquisitions only {b - a:.4f}s apart across threads, "
            f"need >= {min_interval:.4f}s to respect {max_rate}/s"
        )

    # sliding-window check mirroring the actual requirement: in any
    # 1-second window, no more than max_rate acquisitions occurred.
    for i, start in enumerate(timestamps):
        count_in_window = sum(1 for t in timestamps[i:] if t < start + 1.0)
        assert count_in_window <= max_rate + 1, f"{count_in_window} calls within 1s of {start}, exceeds {max_rate}/s"


def test_sec_download_rate_limiter_default_is_10_per_second() -> None:
    """scripts/162_download_xbrl_only_filing.py wires its RateLimiter to
    SEC's own documented fair-access limit -- 10 requests/second. This
    guards against a future edit accidentally raising that constant
    without noticing it changes the project's binding SEC rate policy."""
    import importlib.util
    import sys
    from pathlib import Path

    project_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("s162_rate_check", project_dir / "scripts" / "162_download_xbrl_only_filing.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["s162_rate_check"] = module
    spec.loader.exec_module(module)

    assert module.REQUEST_DELAY_SECONDS == 0.1
    assert module.SEC_RATE_LIMITER._min_interval == 0.1
