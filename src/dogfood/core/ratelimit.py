"""In-memory sliding-window rate limiter. See contracts/t3-public.md cases 18-20.

State lives in the process: a restart clears it (documented). Time is injected.
"""

import threading
from collections import deque


class SlidingWindow:
    def __init__(self, limit: int, window_s: float):
        if limit < 1 or window_s <= 0:
            raise ValueError("limit must be >= 1 and window positive")
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def hit(self, key: str, now: float) -> tuple[bool, float]:
        """Record an attempt. Returns (allowed, retry_after_seconds). Refused attempts are not recorded."""
        with self._lock:
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
