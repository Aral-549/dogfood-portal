"""In-memory sliding-window rate limiter. See contracts/t3-public.md cases 18-20.

State lives in the process: a restart clears it (documented). Time is injected.
Keys whose hits have all expired are swept, so a flood of distinct keys (random emails,
spoofed IPs) cannot grow memory without bound.
"""

import threading
from collections import deque

SWEEP_EVERY = 1024  # hits between sweeps of expired keys


class SlidingWindow:
    enabled = True

    def __init__(self, limit: int, window_s: float):
        if limit < 1 or window_s <= 0:
            raise ValueError("limit must be >= 1 and window positive")
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._since_sweep = 0

    def _trim(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def _sweep(self, now: float) -> None:
        self._since_sweep += 1
        if self._since_sweep < SWEEP_EVERY:
            return
        self._since_sweep = 0
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
            del self._hits[key]

    def __len__(self) -> int:
        """Number of keys currently tracked (for tests and diagnostics)."""
        return len(self._hits)

    def hit(self, key: str, now: float) -> tuple[bool, float]:
        """Record an attempt. Returns (allowed, retry_after_seconds). Refused attempts are not recorded."""
        with self._lock:
            self._sweep(now)
            q = self._hits.setdefault(key, deque())
            self._trim(q, now)
            if len(q) >= self.limit:
                return False, max(0.0, q[0] + self.window - now)
            q.append(now)
            return True, 0.0

    def count(self, key: str, now: float) -> int:
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            self._trim(q, now)
            return len(q)

    def blocked(self, key: str, now: float) -> tuple[bool, float]:
        """True if `key` is at its limit, without recording anything."""
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return False, 0.0
            self._trim(q, now)
            if len(q) >= self.limit:
                return True, max(0.0, q[0] + self.window - now)
            return False, 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class Unlimited(SlidingWindow):
    """DOGFOOD_RATE_LIMITS=off (load tests only): never limits, remembers nothing."""
    enabled = False

    def __init__(self, limit: int = 1, window_s: float = 1):
        super().__init__(limit, window_s)

    def hit(self, key: str, now: float) -> tuple[bool, float]:
        return True, 0.0

    def blocked(self, key: str, now: float) -> tuple[bool, float]:
        return False, 0.0
