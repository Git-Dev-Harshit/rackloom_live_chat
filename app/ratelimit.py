"""Fixed-window in-memory limiter, the same model as web/src/lib/ratelimit.ts.

Good for the single process this service runs as (one uvicorn worker). A restart forgets the
counters, and a second worker or host would keep its own: move the buckets to Redis or Postgres
before running more than one.
"""
from __future__ import annotations

import math
import threading
import time


class RateLimiter:
    def __init__(self, clock=time.monotonic, max_keys: int = 50_000):
        self._buckets: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Count one attempt. Returns (allowed, seconds until the window resets when refused)."""
        now = self._clock()
        with self._lock:
            count, reset_at = self._buckets.get(key, (0, 0.0))
            if reset_at <= now:
                count, reset_at = 0, now + window_s
                if len(self._buckets) >= self._max_keys:
                    self._buckets = {k: v for k, v in self._buckets.items() if v[1] > now}
            count += 1
            self._buckets[key] = (count, reset_at)
            if count > limit:
                return False, max(1, math.ceil(reset_at - now))
            return True, 0

    def blocked(self, key: str, limit: int) -> tuple[bool, int]:
        """Whether `key` has used up `limit` in its current window, without counting this look.
        For limits on failures: check first, `hit` only when an attempt fails."""
        now = self._clock()
        with self._lock:
            count, reset_at = self._buckets.get(key, (0, 0.0))
        if reset_at > now and count >= limit:
            return True, max(1, math.ceil(reset_at - now))
        return False, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
